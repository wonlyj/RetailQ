import pytest
from retailq.config import Settings
from retailq.tools.runtime import ToolContext, build_registry
from tests.test_rag_answer import retrieval

pytestmark = pytest.mark.anyio


async def test_query_tool_uses_original_question_once():
    calls = []
    class Repository:
        def rag_retrieve(self, raw, **kwargs):
            calls.append(raw)
            return retrieval(1)
    context = ToolContext(1, 'u', 'tool', raw_question='原始问题 带型号 MH-LP100')
    registry = build_registry(Repository(), context, Settings(_env_file=None))
    outcome = await registry.execute({'name':'query_faq','args':{'keyword':'型号'},'id':'tool'})
    assert outcome.payload['ok'] and calls == ['原始问题 带型号 MH-LP100']
    assert context.retrieval_result is not None


async def test_timed_out_retrieval_keeps_ownership_until_thread_finishes():
    import asyncio
    from threading import Event
    started, finish = Event(), Event()
    class Repository:
        def rag_retrieve(self, raw, **kwargs):
            started.set()
            finish.wait(3)
            return retrieval(1)
    context = ToolContext(1, 'u', 'tool', raw_question='问题')
    registry = build_registry(Repository(), context, Settings(_env_file=None, knowledge_query_timeout_seconds=.05))
    outcome = await registry.execute({'name':'query_faq','args':{'keyword':'问题'},'id':'tool'})
    assert started.is_set() and not outcome.payload['ok'] and registry.pending
    finish.set()
    await asyncio.wait_for(registry.drain(), timeout=3)
    assert not registry.pending


async def test_invented_category_is_not_silently_applied():
    class Repository:
        def rag_retrieve(self, raw, **kwargs): raise AssertionError('must not search')
    context = ToolContext(1, 'u', 'tool', raw_question='运费是多少')
    registry = build_registry(Repository(), context, Settings(_env_file=None))
    outcome = await registry.execute({'name':'query_faq','args':{'keyword':'运费', 'category':'智能猫砂盆'},'id':'tool'})
    assert not outcome.payload['ok']


@pytest.mark.parametrize(('question','expected'),[
    ('MH-LP100适用猫咪体重是多少？',None),
    ('请只查FAQ，MH-LP100适用猫咪体重是多少？','faq'),
])
async def test_unconfirmed_content_type_does_not_narrow_retrieval(question,expected):
    calls=[]
    class Repository:
        def rag_retrieve(self, raw, **kwargs):
            calls.append(kwargs)
            return retrieval(1)
    registry=build_registry(Repository(),ToolContext(1,'u','tool',raw_question=question),Settings(_env_file=None))
    outcome=await registry.execute({'name':'query_faq','args':{'keyword':'MH-LP100','content_type':'faq'},'id':'tool'})
    assert outcome.payload['ok'] and calls[0]['content_type']==expected
