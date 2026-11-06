import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from functools import partial
import json
import logging
from pathlib import Path
import time

import anyio
from langchain_core.messages import AIMessage,HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from retailq.api.sources import public_citation
from retailq.context import prepare_tool_chat
from retailq.errors import ServiceError
from retailq.gateway import public_error
from retailq.services.chat import ChatEvent
from retailq.tools.runtime import ToolContext,build_registry
from retailq.workflow.agent import READ_TOOLS

logger=logging.getLogger('retailq.workflow')


@dataclass
class PreparedWorkflowTurn:
    ref: object
    user_id: str
    raw_question: str
    prepared: object
    registry: object
    lease: object
    deadline: float
    graph: object=None
    state: object=None
    evidence: object=None
    agent: object=None
    released: bool=False

    @property
    def conversation_id(self):return self.ref.conversation_id


class WorkflowChatService:
    def __init__(self,runtime):
        self.runtime=runtime
        self.repository=runtime.chat_repository
        self.workflow_repository=runtime.repository
        self.gateway,self.settings,self.coordinator=runtime.gateway,runtime.settings,runtime.coordinator
        self.busy=set();self.background=set()
        self.prompt=(Path(__file__).resolve().parents[1]/'prompts/workflow_agent.txt').read_text(encoding='utf-8')

    async def _write(self,fn,*args,**kwargs):
        with anyio.CancelScope(shield=True):
            return await anyio.to_thread.run_sync(partial(fn,*args,**kwargs))

    async def prepare(self,body):
        cid=body.conversation_id;lease=None;turn=None
        try:
            if cid is not None:
                if cid in self.busy: raise ServiceError('conversation_busy',409,'该会话仍在处理上一轮，请稍后再试。')
                lease=self.coordinator.try_acquire('conversation',str(cid))
                if lease is None:raise ServiceError('conversation_busy',409,'该会话仍在处理上一轮，请稍后再试。')
                turns=await asyncio.to_thread(self.workflow_repository.load_projection,cid,body.user_id)
            else:
                turns=[[HumanMessage(content=body.history[i].content),AIMessage(content=body.history[i+1].content)]
                       for i in range(0,len(body.history),2)]
            registry=build_registry(self.repository,ToolContext(cid or 0,body.user_id,raw_question=body.message),self.settings)
            schemas=[convert_to_openai_tool(t) for t in registry.tools if t.name in READ_TOOLS]
            prepared=prepare_tool_chat(body.message,turns,schemas,self.settings,system_prompt=self.prompt)
            ref=await self._write(self.workflow_repository.start_turn,body)
            cid=ref.conversation_id
            if lease is None:
                lease=self.coordinator.try_acquire('conversation',str(cid))
                if lease is None:raise ServiceError('conversation_busy',409,'该会话正在处理。')
            self.busy.add(cid)
            registry.context.conversation_id=cid
            turn=PreparedWorkflowTurn(ref,body.user_id,body.message,prepared,registry,lease,
                                      time.monotonic()+self.settings.workflow_turn_timeout_seconds)
            turn.graph=self.runtime.graph_for(turn)
            turn.state=await self.runtime.rebase(turn,turns)
            return turn
        except BaseException:
            if lease is not None:lease.close()
            if cid is not None:self.busy.discard(cid)
            raise

    async def stream(self,turn):
        yield ChatEvent('meta',{'conversation_id':turn.conversation_id,'turn_id':turn.ref.turn_id,
            'input_tokens_estimate':turn.prepared.input_tokens_estimate,
            'dropped_history_messages':turn.prepared.dropped_history_messages})
        config=self.runtime.config_for(turn.conversation_id)
        try:
            async with asyncio.timeout(max(0,turn.deadline-time.monotonic())):
                async with aclosing(self._graph_events(turn,config)) as stream:
                    async for event in stream:
                        yield ChatEvent(event['event'],event['data'])
                snapshot=await turn.graph.aget_state(config)
                state=snapshot.values
                if snapshot.next:raise ServiceError('workflow_incomplete',502,'工作流尚未完整结束。')
                if state.get('finish_reason')=='length':
                    yield ChatEvent('done',{'finish_reason':'length'});return
                if not state.get('final_message_id'):raise ServiceError('workflow_incomplete',502,'答复尚未保存。')
                if state.get('evidence',{}).get('citations'):
                    citations=await asyncio.to_thread(self.workflow_repository.load_citations,turn.conversation_id,
                                                       turn.user_id,'wf_'+turn.ref.turn_id)
                    yield ChatEvent('sources',{'message_id':str(state['final_message_id']),'turn_id':turn.ref.turn_id,
                        'citations':[public_citation(c).model_dump() for c in citations]})
                actions=await asyncio.to_thread(self.workflow_repository.list_actions,turn.conversation_id,turn.user_id,state['final_message_id'])
                yield ChatEvent('actions',{'message_id':str(state['final_message_id']),'turn_id':turn.ref.turn_id,
                                           'conversation_id':turn.conversation_id,'actions':list(actions)})
                logger.info(json.dumps({'turn_id':turn.ref.turn_id,'conversation_id':turn.conversation_id,
                    'event':'complete','status':state.get('status'),'intent':state.get('intent'),'route':state.get('route'),
                    'tool_steps':state.get('agent',{}).get('step',0),'model_calls':state.get('agent',{}).get('model_calls',0),
                    'usage':state.get('agent',{}).get('usage')},ensure_ascii=False))
                yield ChatEvent('done',{'finish_reason':'stop'})
        except asyncio.CancelledError:
            await self._write(self.workflow_repository.mark_terminal,turn.ref,'interrupted','client_disconnected')
            raise
        except Exception as exc:
            error=public_error(exc)
            await self._write(self.workflow_repository.mark_terminal,turn.ref,'failed',error.code)
            logger.info(json.dumps({'event':'error','turn_id':turn.ref.turn_id,'code':error.code}))
            yield ChatEvent('error',{'code':error.code,'message':error.public_message})

    async def _graph_events(self,turn,config):
        # Cancelling the producer task propagates cancellation into graph nodes.
        # Merely aclosing an astream suspended at a custom yield can leave its
        # model node running until the upstream timeout. Keep backpressure bounded.
        queue=asyncio.Queue(maxsize=1)
        async def produce():
            try:
                async with aclosing(turn.graph.astream(turn.state,config,stream_mode='custom',durability='sync')) as stream:
                    async for event in stream:await queue.put(event)
            except Exception as exc:await queue.put(exc)
            await queue.put(None)
        task=asyncio.create_task(produce())
        try:
            while True:
                item=await queue.get()
                if item is None:break
                if isinstance(item,Exception):raise item
                yield item
        finally:
            task.cancel()
            with anyio.CancelScope(shield=True):await asyncio.gather(task,return_exceptions=True)

    async def release(self,turn):
        if turn.released:return
        turn.released=True
        async def settle():
            try:
                await turn.registry.drain()
                if turn.evidence is not None:await turn.evidence.drain()
            finally:
                self.busy.discard(turn.conversation_id);turn.lease.close()
        if turn.registry.pending or (turn.evidence and turn.evidence.pending):
            task=asyncio.create_task(settle());self.background.add(task);task.add_done_callback(self.background.discard)
        else:await settle()

    async def aclose(self):
        if self.background:await asyncio.gather(*list(self.background),return_exceptions=True)
