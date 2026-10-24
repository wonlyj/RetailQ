import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from functools import partial
import json

import anyio
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from retailq.context import PreparedChat, history_turns, prepare_tool_answer, prepare_tool_chat
from retailq.errors import ServiceError
from retailq.gateway import public_error, unsupported
from retailq.schemas import ChatRequest
from retailq.tools.runtime import ToolContext, ToolRegistry, build_registry


@dataclass
class PreparedTurn:
    conversation_id: int
    user_id: str
    prepared: PreparedChat
    prior_call_ids: set[str]
    registry: ToolRegistry
    released: bool = False


@dataclass(frozen=True)
class ChatEvent:
    event: str
    data: dict


class ChatService:
    """One process, one active turn per conversation; transactions stay short."""
    def __init__(self, repository, gateway, settings):
        self.repository, self.gateway, self.settings = repository, gateway, settings
        self.busy: set[int] = set()
        self.background: set[asyncio.Task] = set()

    async def _write(self, method, *args):
        # Shield only short database operations, never model generation.
        with anyio.CancelScope(shield=True):
            return await anyio.to_thread.run_sync(partial(method,*args))

    async def prepare(self, body: ChatRequest) -> PreparedTurn:
        cid = body.conversation_id
        if cid is not None:
            if cid in self.busy:
                raise ServiceError('conversation_busy',409,'该会话仍在处理上一轮，请稍后再试。')
            self.busy.add(cid)
        try:
            records = await asyncio.to_thread(self.repository.load_messages,cid,body.user_id) if cid is not None else []
            if cid is None:
                turns = [[HumanMessage(content=body.history[i].content),AIMessage(content=body.history[i+1].content)]
                         for i in range(0,len(body.history),2)]
            else:
                turns = history_turns(records)
            registry = build_registry(self.repository,ToolContext(cid or 0,body.user_id),self.settings)
            schemas = [convert_to_openai_tool(t) for t in registry.tools]
            prepared = prepare_tool_chat(body.message,turns,schemas,self.settings)
            cid = await self._write(self.repository.start_turn,body.user_id,cid,body.history,body.message)
            self.busy.add(cid)
            prior_ids = {c['id'] for r in records for c in (r.tool_calls or []) if c.get('id')}
            return PreparedTurn(cid,body.user_id,prepared,prior_ids,registry)
        except BaseException:
            if cid is not None:
                self.busy.discard(cid)
            raise

    async def stream(self, turn: PreparedTurn):
        yield ChatEvent('meta',{'conversation_id':turn.conversation_id,
            'input_tokens_estimate':turn.prepared.input_tokens_estimate,
            'dropped_history_messages':turn.prepared.dropped_history_messages})
        try:
            selection = await self.gateway.select_tool(turn.prepared.messages,turn.registry.tools)
            result = None
            if selection.tool_calls:
                if any(c['id'] in turn.prior_call_ids for c in selection.tool_calls):
                    raise ServiceError('reused_tool_call_id',502,'模型重复使用已有工具申请，未执行操作。')
                wire = [{'id':c['id'],'type':'function','function':{'name':c['name'],
                    'arguments':json.dumps(c['args'],ensure_ascii=False)}} for c in selection.tool_calls]
                await self._write(self.repository.append_message,turn.conversation_id,turn.user_id,
                                  'assistant',selection.content or None,wire)
                if len(selection.tool_calls) != 1:
                    raise ServiceError('multiple_tool_calls',502,'本章每轮仅支持一个工具，本轮未执行工具。')
                call = selection.tool_calls[0]
                turn.registry = build_registry(self.repository,ToolContext(turn.conversation_id,turn.user_id,call['id']),self.settings)
                status = {'tool_call_id':call['id'],'name':call['name']}
                yield ChatEvent('tool_status',{**status,'status':'running'})
                outcome = await turn.registry.execute(call)
                content = json.dumps(outcome.payload,ensure_ascii=False)
                if outcome.receipt_state == 'unrecorded':
                    await self._write(self.repository.append_message,turn.conversation_id,turn.user_id,'tool',content,None,call['id'])
                result = ToolMessage(content=content,tool_call_id=call['id'],name=call['name'])
                state = {**status,'status':'completed' if outcome.payload['ok'] else 'failed'}
                if not outcome.payload['ok']:
                    state['code'] = outcome.payload['error']['code']
                yield ChatEvent('tool_status',state)
            messages = prepare_tool_answer(turn.prepared,selection,result,self.settings)
            text, finish_reason = [], None
            async with aclosing(self.gateway.stream(messages)) as stream:
                async for chunk in stream:
                    if chunk.text:
                        text.append(chunk.text)
                        yield ChatEvent('token',{'text':chunk.text})
                    if chunk.finish_reason:
                        finish_reason = chunk.finish_reason
            if not text:
                raise unsupported()
            if finish_reason is None:
                raise ServiceError('incomplete_upstream_stream',502,'模型回复未完整结束，请重试。')
            if finish_reason == 'stop':
                await self._write(self.repository.append_message,turn.conversation_id,turn.user_id,'assistant',''.join(text))
            yield ChatEvent('done',{'finish_reason':finish_reason})
        except Exception as exc:
            error = public_error(exc)
            yield ChatEvent('error',{'code':error.code,'message':error.public_message})

    async def release(self, turn: PreparedTurn):
        if turn.released:
            return
        turn.released = True
        if turn.registry.pending:
            async def settle():
                try:
                    await turn.registry.drain()
                finally:
                    self.busy.discard(turn.conversation_id)
            task = asyncio.create_task(settle())
            self.background.add(task)
            task.add_done_callback(self.background.discard)
        else:
            self.busy.discard(turn.conversation_id)

    async def aclose(self):
        if self.background:
            await asyncio.gather(*list(self.background),return_exceptions=True)
