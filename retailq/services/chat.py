import asyncio
from contextlib import aclosing
from dataclasses import asdict, dataclass
from functools import partial
import json
from pathlib import Path

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
    raw_question: str = ''
    lease: object | None = None
    released: bool = False


@dataclass(frozen=True)
class ChatEvent:
    event: str
    data: dict


class ChatService:
    """One process, one active turn per conversation; transactions stay short."""
    def __init__(self, repository, gateway, settings, *, rag_gateway=None, rag_repository=None, confidence_policy=None, coordinator=None):
        self.repository, self.gateway, self.settings = repository, gateway, settings
        self.rag_gateway, self.rag_repository, self.confidence_policy = rag_gateway, rag_repository, confidence_policy
        self.coordinator = coordinator
        self.busy: set[int] = set()
        self.background: set[asyncio.Task] = set()

    async def _write(self, method, *args, **kwargs):
        # Shield only short database operations, never model generation.
        with anyio.CancelScope(shield=True):
            return await anyio.to_thread.run_sync(partial(method,*args,**kwargs))

    async def prepare(self, body: ChatRequest) -> PreparedTurn:
        cid = body.conversation_id
        lease = None
        if cid is not None:
            if cid in self.busy:
                raise ServiceError('conversation_busy',409,'该会话仍在处理上一轮，请稍后再试。')
            self.busy.add(cid)
        try:
            if cid is not None and self.coordinator is not None:
                lease = self.coordinator.try_acquire('conversation', str(cid))
                if lease is None: raise ServiceError('conversation_busy',409,'该会话仍在处理上一轮，请稍后再试。')
            records = await asyncio.to_thread(self.repository.load_messages,cid,body.user_id) if cid is not None else []
            if cid is None:
                turns = [[HumanMessage(content=body.history[i].content),AIMessage(content=body.history[i+1].content)]
                         for i in range(0,len(body.history),2)]
            else:
                turns = history_turns(records)
            registry = build_registry(self.repository,ToolContext(cid or 0,body.user_id,raw_question=body.message),self.settings)
            schemas = [convert_to_openai_tool(t) for t in registry.tools]
            prepared = prepare_tool_chat(body.message,turns,schemas,self.settings)
            cid = await self._write(self.repository.start_turn,body.user_id,cid,body.history,body.message)
            if lease is None and self.coordinator is not None:
                lease = self.coordinator.try_acquire('conversation', str(cid))
                if lease is None: raise ServiceError('conversation_busy',409,'该会话仍在处理上一轮，请稍后再试。')
            self.busy.add(cid)
            prior_ids = {c['id'] for r in records for c in (r.tool_calls or []) if c.get('id')}
            return PreparedTurn(cid,body.user_id,prepared,prior_ids,registry,body.message,lease)
        except BaseException:
            if lease is not None: lease.close()
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
                turn.registry = build_registry(self.repository,ToolContext(turn.conversation_id,turn.user_id,call['id'],turn.raw_question),self.settings)
                status = {'tool_call_id':call['id'],'name':call['name']}
                yield ChatEvent('tool_status',{**status,'status':'running'})
                outcome = await turn.registry.execute(call)
                if call['name'] == 'query_faq' and getattr(self.repository, 'rag_retrieve', None) is not None:
                    if not outcome.payload['ok']:
                        raise ServiceError(outcome.payload['error']['code'],503,outcome.payload['error']['message'])
                    async for event in self._rag_answer(turn, call, outcome):
                        yield event
                    return
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
            if finish_reason not in ('stop','length'):
                raise ServiceError('unsupported_finish_reason',502,'模型回复异常结束，请重试或核查模型服务。')
            if finish_reason == 'stop':
                await self._write(self.repository.append_message,turn.conversation_id,turn.user_id,'assistant',''.join(text))
            yield ChatEvent('done',{'finish_reason':finish_reason})
        except Exception as exc:
            error = public_error(exc)
            yield ChatEvent('error',{'code':error.code,'message':error.public_message})

    async def _rag_answer(self, turn, call, outcome):
        from retailq.knowledge.answer import prepare_bundle, validate_decision
        if self.rag_gateway is None or self.rag_repository is None or self.confidence_policy is None:
            raise ServiceError('rag_policy_unavailable',503,'知识回答尚未完成校准或配置。')
        result = turn.registry.context.retrieval_result
        if result is None: raise ServiceError('knowledge_retrieval_unavailable',503,'知识查询结果不可用。')
        bundle = None
        allowed = self.confidence_policy.allows(result)
        if allowed:
            prompt = (Path(__file__).resolve().parents[1] / 'prompts' / 'rag_answer.txt').read_text(encoding='utf-8')
            bundle = prepare_bundle(result, system_prompt=prompt, input_budget=self.settings.input_budget)
        receipt = dict(outcome.payload)
        receipt['rag_evidence'] = {'bundle_hash':bundle.bundle_hash if bundle else None,
                                   'citations':[asdict(c) for c in bundle.citations] if bundle else []}
        content = json.dumps(receipt,ensure_ascii=False)
        await self._write(self.repository.append_message,turn.conversation_id,turn.user_id,'tool',content,None,call['id'])
        yield ChatEvent('tool_status',{'tool_call_id':call['id'],'name':call['name'],'status':'completed'})
        source, reason = None, None
        if not allowed:
            answer, source, reason = '知识库没有足够可靠的依据，暂时无法回答这个问题。', 'retrieval_low_conf', '检索证据不足或低于校准门槛'
        else:
            decision = await self.rag_gateway.answer(bundle)
            validate_decision(decision, bundle)
            if decision.useful:
                answer = decision.answer
            else:
                answer, source, reason = '知识库中的信息不足以回答这个问题，请查看官方规则或联系人工客服。', 'self_check', decision.reason
        message_id = await self._write(self.rag_repository.finalize,turn.conversation_id,turn.user_id,call['id'],
                                       answer=answer,source=source,reason=reason)
        if source is None:
            yield ChatEvent('sources',{'message_id':str(message_id),'citations':[asdict(c) for c in bundle.citations]})
        yield ChatEvent('token',{'text':answer})
        yield ChatEvent('done',{'finish_reason':'stop'})

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
                    if turn.lease is not None: turn.lease.close()
            task = asyncio.create_task(settle())
            self.background.add(task)
            task.add_done_callback(self.background.discard)
        else:
            self.busy.discard(turn.conversation_id)
            if turn.lease is not None: turn.lease.close()

    async def aclose(self):
        if self.background:
            await asyncio.gather(*list(self.background),return_exceptions=True)
