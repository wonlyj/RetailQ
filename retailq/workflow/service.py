import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from functools import partial
import json
import logging
from pathlib import Path
import time
from types import SimpleNamespace
from langgraph.types import Command

import anyio
from langchain_core.messages import AIMessage,HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from retailq.api.sources import public_citation
from retailq.context import prepare_tool_chat
from retailq.errors import ServiceError
from retailq.gateway import public_error
from retailq.services.chat import ChatEvent
from retailq.tools.runtime import ToolContext,build_registry
from retailq.workflow.agent import READ_TOOLS,stage_reserve
from retailq.workflow.sql import write_sql
from retailq.workflow.contracts import TurnRef
from retailq.workflow.pending import PendingTurns,pending_from_snapshot,public_pending,recovery_error
from retailq.workflow.query_processing import resolve_output_cap

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
    replay_state: object=None

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
        self.pending_turns=PendingTurns(self.workflow_repository,runtime)

    async def _write(self,fn,*args,**kwargs):
        return await write_sql(fn,*args,**kwargs)

    async def prepare(self,body):
        cid=body.conversation_id;lease=None;turn=None;ref=None
        try:
            resolve_output_cap(body.message,self.settings)
            if cid is not None:
                if cid in self.busy: raise ServiceError('conversation_busy',409,'该会话仍在处理上一轮，请稍后再试。')
                lease=self.coordinator.try_acquire('conversation',str(cid))
                if lease is None:raise ServiceError('conversation_busy',409,'该会话仍在处理上一轮，请稍后再试。')
                if await self.pending_turns.read(cid,body.user_id):
                    raise ServiceError('pending_order_selection',409,'请先选择订单或取消当前订单选择，再发送新消息。')
                await self._write(self.workflow_repository.interrupt_active,cid,body.user_id)
                turns=await asyncio.to_thread(self.workflow_repository.load_projection,cid,body.user_id)
            else:
                turns=[[HumanMessage(content=body.history[i].content),AIMessage(content=body.history[i+1].content)]
                       for i in range(0,len(body.history),2)]
            registry=build_registry(self.repository,ToolContext(cid or 0,body.user_id,raw_question=body.message),self.settings)
            schemas=[convert_to_openai_tool(t) for t in registry.tools if t.name in READ_TOOLS]
            schema_extra=len(json.dumps(schemas,ensure_ascii=False).encode('utf-8'))-len(json.dumps(schemas,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
            prepared=prepare_tool_chat(body.message,turns,schemas,self.settings,system_prompt=self.prompt,
                reserved_tokens=stage_reserve()+schema_extra)
            def start_turn():
                nonlocal ref
                ref=self.workflow_repository.start_turn(body)
                return ref
            await self._write(start_turn)
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
        except BaseException as exc:
            try:
                if ref is not None:
                    await self._write(self.workflow_repository.mark_terminal,ref,
                        'interrupted' if isinstance(exc,asyncio.CancelledError) else 'failed',
                        getattr(exc,'code','workflow_prepare_failed'))
            finally:
                if lease is not None:lease.close()
                if cid is not None:self.busy.discard(cid)
            raise

    async def pending(self,cid,user_id):
        lease=self.coordinator.try_acquire('conversation',str(cid))
        if lease is None:raise ServiceError('conversation_busy',409,'该会话正在处理，请稍后核查。')
        try:return public_pending(await self.pending_turns.read(cid,user_id))
        finally:lease.close()

    async def turn_status(self,cid,turn_id,user_id):
        row=await self._write(self.workflow_repository.get_turn,turn_id,user_id)
        if row['conversation_id']!=cid:raise ServiceError('workflow_turn_not_found',404,'运行不存在。')
        lease=self.coordinator.try_acquire('conversation',str(cid))
        if lease is not None:
            try:
                if row['status'] in ('active','waiting_order'):
                    pending=await self.pending_turns.read(cid,user_id)
                    if pending is None and row['status']=='active':
                        snapshot=await self.runtime.snapshot(cid)
                        if snapshot.values.get('turn_id')==turn_id and snapshot.next:
                            await self._write(self.workflow_repository.mark_terminal,TurnRef(turn_id,cid,row['user_message_id']),
                                              'failed','resume_execution_incomplete')
                    row=await self._write(self.workflow_repository.get_turn,turn_id,user_id)
            finally:lease.close()
        return {'turn_id':turn_id,'status':row['status'],'message_id':row['final_message_id'],
                'answer':row['payload'].get('final',{}).get('answer')}

    async def prepare_resume(self,cid,turn_id,body):
        lease=self.coordinator.try_acquire('conversation',str(cid));turn=None
        if lease is None:raise ServiceError('conversation_busy',409,'该会话仍在处理，请先核查运行状态。')
        try:
            row=await self._write(self.workflow_repository.get_turn,turn_id,body.user_id)
            if row['conversation_id']!=cid:raise ServiceError('workflow_turn_not_found',404,'运行不存在。')
            ref=TurnRef(turn_id,cid,row['user_message_id'])
            completed=row['status'] in ('completed','limited','cancelled')
            remaining=0
            if not completed:
                pending=await self.pending_turns.read(cid,body.user_id)
                if pending is None or pending['turn_id']!=turn_id or pending['interrupt_id']!=body.interrupt_id:
                    raise recovery_error()
                if not body.cancel and body.order_id not in {o['order_id'] for o in pending['options']}:
                    raise ServiceError('order_selection_invalid',409,'请选择当前卡片中的订单。')
                refreshed=await self._write(self.workflow_repository.get_turn,turn_id,body.user_id)
                remaining=min(pending['pause_budget']['remaining_active_seconds'],refreshed['payload']['remaining_active_seconds'])
                if remaining<=0 and not body.cancel:raise ServiceError('workflow_timeout',409,'本轮执行时间已耗尽，请取消后重试。')
                if body.cancel:remaining=max(remaining,5)  # Cleanup only; graph cannot enter model/business nodes.
            claim=await self._write(self.workflow_repository.claim_resume,ref,body.user_id,body.interrupt_id,body.request_key,body.selection())
            registry=build_registry(self.repository,ToolContext(cid,body.user_id,raw_question=row['raw_question']),self.settings)
            turn=PreparedWorkflowTurn(ref,body.user_id,row['raw_question'],SimpleNamespace(input_tokens_estimate=0,dropped_history_messages=0),
                registry,lease,time.monotonic()+remaining)
            if claim['completed']:
                turn.replay_state={**row['payload'].get('final',{}),'final_message_id':row['final_message_id'],
                                   'status':row['status'],'finish_reason':'stop'}
                turn.deadline=time.monotonic()+self.settings.workflow_turn_timeout_seconds
            else:
                turn.graph=self.runtime.graph_for(turn);turn.state=Command(resume=body.selection())
            self.busy.add(cid)
            return turn
        except BaseException:
            lease.close();raise

    async def stream(self,turn):
        yield ChatEvent('meta',{'conversation_id':turn.conversation_id,'turn_id':turn.ref.turn_id,
            'replayed':turn.replay_state is not None,
            'input_tokens_estimate':turn.prepared.input_tokens_estimate,
            'dropped_history_messages':turn.prepared.dropped_history_messages})
        config=self.runtime.config_for(turn.conversation_id)
        try:
            async with asyncio.timeout(max(0,turn.deadline-time.monotonic())):
                if turn.replay_state is not None:
                    state=turn.replay_state
                    if state['status']!='cancelled':yield ChatEvent('token',{'text':state['answer']})
                else:
                    async with aclosing(self._graph_events(turn,config)) as stream:
                        async for event in stream:yield ChatEvent(event['event'],event['data'])
                    snapshot=await turn.graph.aget_state(config);state=snapshot.values
                    pending=await self.pending_turns.reconcile(turn.ref,snapshot)
                    if pending is not None:
                        yield ChatEvent('order_selector',public_pending(pending))
                        await self.release(turn)
                        yield ChatEvent('paused',{k:pending[k] for k in ('conversation_id','turn_id','interrupt_id')})
                        return
                    if snapshot.next:raise ServiceError('workflow_incomplete',502,'工作流尚未完整结束。')
                if state.get('status')=='cancelled':
                    await self.release(turn)
                    yield ChatEvent('cancelled',{'conversation_id':turn.conversation_id,'turn_id':turn.ref.turn_id});return
                if state.get('finish_reason')=='length':
                    await self.release(turn)
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
                await self.release(turn)
                yield ChatEvent('done',{'finish_reason':'stop'})
        except asyncio.CancelledError:
            if not await self._has_pause(turn):await self._write(self.workflow_repository.mark_terminal,turn.ref,'interrupted','client_disconnected')
            raise
        except Exception as exc:
            error=public_error(exc)
            if not await self._has_pause(turn):await self._write(self.workflow_repository.mark_terminal,turn.ref,'failed',error.code)
            logger.info(json.dumps({'event':'error','turn_id':turn.ref.turn_id,'code':error.code}))
            yield ChatEvent('error',{'code':error.code,'message':error.public_message})

    async def _has_pause(self,turn):
        if turn.replay_state is not None:return False
        snapshot=await self.runtime.snapshot(turn.conversation_id)
        pending=pending_from_snapshot(snapshot,self.settings.workflow_turn_timeout_seconds)
        return pending is not None and pending['turn_id']==turn.ref.turn_id

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
                if turn.replay_state is None:
                    snapshot=await self.runtime.snapshot(turn.conversation_id)
                    if not await self.pending_turns.reconcile(turn.ref,snapshot):
                        await self._write(self.workflow_repository.mark_terminal,turn.ref,'interrupted','request_ended')
            finally:
                self.busy.discard(turn.conversation_id);turn.lease.close()
        if turn.registry.pending or (turn.evidence and turn.evidence.pending):
            task=asyncio.create_task(settle());self.background.add(task);task.add_done_callback(self.background.discard)
        else:await settle()

    async def aclose(self):
        if self.background:await asyncio.gather(*list(self.background),return_exceptions=True)
