from types import SimpleNamespace
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from retailq.config import Settings
from retailq.knowledge.answer import RAGDecision
from retailq.knowledge.rag_models import LowConfidenceQuestion
from retailq.knowledge.rag_repository import RAGRepository
from retailq.repositories import ChatRepository
from retailq.schemas import ChatRequest
from retailq.services.chat import ChatService
from tests.test_rag_answer import retrieval

pytestmark = [pytest.mark.mysql, pytest.mark.anyio]


async def test_self_check_refusal_stores_original_question(test_database):
    calls = []
    repo = ChatRepository(test_database)
    def retrieve(raw, **kwargs):
        calls.append(raw)
        result = retrieval(1)
        result.understanding = SimpleNamespace(raw_question=raw)
        return result
    repo.rag_retrieve = retrieve
    class Selector:
        async def select_tool(self, messages, tools):
            return AIMessage(content='', tool_calls=[{'id':'call1','name':'query_faq','args':{'keyword':'退货'}}])
    class Answer:
        async def answer(self, bundle):
            return RAGDecision(useful=False, answer='', reason='证据不足', citations=[])
    class Policy:
        def allows(self, result): return True
    service = ChatService(repo, Selector(), Settings(_env_file=None), rag_gateway=Answer(), rag_repository=RAGRepository(test_database), confidence_policy=Policy())
    turn = await service.prepare(ChatRequest(message='签收后48小时能退货吗？', user_id='alice'))
    try:
        events = [event async for event in service.stream(turn)]
    finally:
        await service.release(turn)
    assert calls == ['签收后48小时能退货吗？']
    assert events[-1].event == 'done'
    with test_database.sessions() as session:
        pool = session.scalar(select(LowConfidenceQuestion))
        assert pool.raw_question == '签收后48小时能退货吗？' and pool.source == 'self_check'


@pytest.mark.parametrize('mode', ['empty', 'low', 'error'])
async def test_retrieval_refusal_or_failure_never_streams_unvalidated_answer(test_database, mode):
    repo = ChatRepository(test_database)
    def retrieve(raw, **kwargs):
        if mode == 'error': raise RuntimeError('服务异常')
        result = retrieval(0 if mode == 'empty' else 1)
        result.understanding = SimpleNamespace(raw_question=raw)
        return result
    repo.rag_retrieve = retrieve
    class Selector:
        async def select_tool(self, messages, tools):
            return AIMessage(content='', tool_calls=[{'id':'call1','name':'query_faq','args':{'keyword':'不存在'}}])
    class Answer:
        async def answer(self, bundle): raise AssertionError('must not invoke model')
    class Policy:
        def allows(self, result): return False
    service = ChatService(repo, Selector(), Settings(_env_file=None), rag_gateway=Answer(), rag_repository=RAGRepository(test_database), confidence_policy=Policy())
    turn = await service.prepare(ChatRequest(message='知识库不存在的内容', user_id='alice'))
    try:
        events = [event async for event in service.stream(turn)]
    finally:
        await service.release(turn)
    with test_database.sessions() as session:
        pools = session.scalars(select(LowConfidenceQuestion)).all()
        if mode == 'error':
            assert events[-1].event == 'error' and not pools
            assert not any(event.event == 'token' for event in events)
        else:
            assert events[-1].event == 'done' and len(pools) == 1
            assert pools[0].source == 'retrieval_low_conf' and pools[0].raw_question == '知识库不存在的内容'


async def test_finalization_failure_emits_no_answer_and_no_pool(test_database):
    repo = ChatRepository(test_database)
    repo.rag_retrieve = lambda raw, **kwargs: retrieval(0)
    class Selector:
        async def select_tool(self, messages, tools):
            return AIMessage(content='', tool_calls=[{'id':'call1','name':'query_faq','args':{'keyword':'不存在'}}])
    class Broken:
        def finalize(self, *args, **kwargs): raise RuntimeError('database failure')
    class Policy:
        def allows(self, result): return False
    service = ChatService(repo, Selector(), Settings(_env_file=None), rag_gateway=object(), rag_repository=Broken(), confidence_policy=Policy())
    turn = await service.prepare(ChatRequest(message='不存在', user_id='alice'))
    try:
        events = [event async for event in service.stream(turn)]
    finally:
        await service.release(turn)
    assert events[-1].event == 'error' and not any(event.event == 'token' for event in events)
    with test_database.sessions() as session:
        assert not session.scalars(select(LowConfidenceQuestion)).all()


async def test_conversation_lease_blocks_second_service_before_user_write(test_database, tmp_path):
    from retailq.coordination import Coordinator, RuntimeIdentity
    from retailq.errors import ServiceError
    repo = ChatRepository(test_database)
    cid = repo.start_turn('alice', None, [], '上一条')
    identity = RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', tmp_path/'knowledge')
    coordinator = Coordinator(tmp_path/'locks', identity)
    service1 = ChatService(repo, object(), Settings(_env_file=None), coordinator=coordinator)
    service2 = ChatService(repo, object(), Settings(_env_file=None), coordinator=coordinator)
    turn = await service1.prepare(ChatRequest(message='第一条', user_id='alice', conversation_id=cid))
    try:
        with pytest.raises(ServiceError) as error:
            await service2.prepare(ChatRequest(message='并发消息', user_id='alice', conversation_id=cid))
        assert error.value.status_code == 409
        assert [row.content for row in repo.load_messages(cid, 'alice') if row.role == 'user'] == ['上一条', '第一条']
    finally:
        await service1.release(turn)


async def test_cited_answer_emitted_only_after_database_finalization(test_database):
    repo = ChatRepository(test_database)
    def retrieve(raw, **kwargs):
        result = retrieval(1)
        result.understanding = SimpleNamespace(raw_question=raw)
        return result
    repo.rag_retrieve = retrieve
    class Selector:
        async def select_tool(self, messages, tools):
            return AIMessage(content='', tool_calls=[{'id':'call1','name':'query_faq','args':{'keyword':'运费'}}])
    class Answer:
        async def answer(self, bundle):
            return RAGDecision(useful=True, answer='演示运费为10元[1]。', reason='', citations=[1])
    class Policy:
        def allows(self, result): return True
    service = ChatService(repo, Selector(), Settings(_env_file=None), rag_gateway=Answer(), rag_repository=RAGRepository(test_database), confidence_policy=Policy())
    turn = await service.prepare(ChatRequest(message='运费多少？', user_id='alice'))
    try:
        events = [event async for event in service.stream(turn)]
        assert [event.event for event in events] == ['meta','tool_status','tool_status','sources','token','done']
        assert repo.load_messages(turn.conversation_id, 'alice')[-1].content == '演示运费为10元[1]。'
    finally:
        await service.release(turn)
