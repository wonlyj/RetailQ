import pytest
from langchain_core.messages import HumanMessage,SystemMessage
from retailq.config import Settings
from retailq.errors import ServiceError
from tests.test_rag_answer import retrieval

pytestmark=pytest.mark.anyio


@pytest.mark.parametrize('n,allowed',[(0,False),(1,False),(2,True)])
async def test_retrieval_state_survives_serialization_before_gate(n,allowed):
    import json
    from retailq.workflow.evidence import EvidenceSteps
    class Policy:
        def allows(self,result): return allowed
    settings=Settings(_env_file=None,context_window_tokens=20000)
    steps=EvidenceSteps(lambda q:retrieval(n),Policy(),settings)
    state={'raw_question':'用户问题','messages':[SystemMessage(content='依据证据'),HumanMessage(content='用户问题')]}
    patch=await steps.retrieve(state)
    assert patch['agent']['usage']['entries'][0]['stage']=='retrieval_query'
    # Checkpoint keeps only serializable values, never the retriever/client.
    patch=json.loads(json.dumps(patch,ensure_ascii=False))
    result=await steps.gate({**state,**patch})
    assert result['gate']['allowed'] is allowed
    assert bool(result.get('evidence',{}).get('citations')) is allowed
    if not allowed: assert result['status']=='fallback' and '依据' in result['answer']


async def test_policy_failure_is_not_low_confidence():
    from retailq.workflow.evidence import EvidenceSteps
    class Policy:
        def allows(self,result): raise ServiceError('rag_policy_unavailable',503,'missing')
    steps=EvidenceSteps(lambda q:retrieval(1),Policy(),Settings(_env_file=None))
    state={'raw_question':'用户问题','messages':[HumanMessage(content='用户问题')]}
    patch=await steps.retrieve(state)
    with pytest.raises(ServiceError) as error: await steps.gate({**state,**patch})
    assert error.value.code=='rag_policy_unavailable'


async def test_query_reservation_prevents_starting_retriever():
    from retailq.workflow.evidence import EvidenceSteps
    def retrieve(q): pytest.fail('budget must reject before retrieval')
    steps=EvidenceSteps(retrieve,None,Settings(_env_file=None,workflow_max_total_tokens=1))
    with pytest.raises(ServiceError):
        await steps.retrieve({'raw_question':'用户问题','messages':[HumanMessage(content='用户问题')]})


async def test_reference_checks_allow_subset_but_reject_missing_or_out_of_range():
    from retailq.workflow.evidence import validate_answer_references
    from types import SimpleNamespace
    citations=(SimpleNamespace(number=1),SimpleNamespace(number=2))
    validate_answer_references('政策10元[2]。',citations)
    for text in ('政策见[99]。','政策10元。'):
        with pytest.raises(ServiceError): validate_answer_references(text,citations)


async def test_cancelled_retrieval_is_retained_until_background_work_finishes():
    import asyncio
    from threading import Event
    from retailq.workflow.evidence import EvidenceSteps
    entered,release=Event(),Event()
    def retrieve(q):
        entered.set()
        if not release.wait(3): raise RuntimeError('test did not release')
        return retrieval(1)
    steps=EvidenceSteps(retrieve,None,Settings(_env_file=None))
    task=asyncio.create_task(steps.retrieve({'raw_question':'问题','messages':[HumanMessage(content='问题')]}))
    try:
        assert await asyncio.to_thread(entered.wait,1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        drain=asyncio.create_task(steps.drain())
        await asyncio.sleep(0)
        assert not drain.done()
        release.set()
        await drain
    finally: release.set()
