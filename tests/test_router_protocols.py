import json
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from retailq.config import Settings
from retailq.errors import ServiceError


def test_intent_has_confidence_and_other():
    from retailq.workflow.classification import parse_intent
    result = parse_intent('{"intent":"其他","confidence":0.2}')
    assert result.intent == '其他' and result.confidence == .2


@pytest.mark.parametrize('value', ['true', 'NaN', 'Infinity', '-0.1', '1.1', '"0.5"', '9'*400])
def test_confidence_rejects_non_finite_or_non_numeric(value):
    from retailq.workflow.classification import parse_intent
    with pytest.raises(ServiceError):
        parse_intent('{"intent":"物流","confidence":'+value+'}')


def test_resolution_and_expansion_exact_protocols():
    from retailq.workflow.router_contracts import parse_resolution, parse_queries, parse_query_plan
    assert parse_resolution('{"resolved_question":"订单1001能退吗"}') == '订单1001能退吗'
    assert parse_query_plan('{"core_case":true}').core_case
    assert parse_queries('{"queries":["退货期限政策","商品完好条件"]}') == ('退货期限政策', '商品完好条件')
    for obj in ({'queries':['x']}, {'queries':['x','x']}, {'queries':['x',1]},
                {'queries':['x','y'],'extra':1}, {'queries':['x']*5}):
        with pytest.raises(ServiceError): parse_queries(json.dumps(obj))


@pytest.mark.anyio
async def test_invalid_completion_is_observed_before_parser_error():
    from retailq.gateway import OpenAIModelGateway
    gateway = OpenAIModelGateway(Settings(_env_file=None,openai_api_key='test',openai_model='test'))
    bound = AsyncMock()
    bound.ainvoke.return_value = AIMessage(content='{bad',response_metadata={'finish_reason':'stop'})
    gateway.model = type('Model',(),{'bind':lambda self,**kw: bound})()
    observations=[]
    async def observe(row): observations.append(row)
    try:
        with pytest.raises(ServiceError):
            await gateway.resolve_query([HumanMessage(content='它呢')],max_tokens=256,observer=observe,call_id='case1')
        assert observations[0]['raw_content']=='{bad'
        assert observations[-1]['phase']=='invalid'
        assert {r['call_id'] for r in observations}=={'case1'}
    finally: await gateway.aclose()


def test_output_cap_and_resolved_message_preserve_history():
    from retailq.workflow.query_processing import resolve_output_cap, resolved_messages
    question='订单1001的'+('完整问题'*30)
    settings=Settings(_env_file=None,max_output_tokens=2048)
    assert resolve_output_cap(question,settings)>256
    with pytest.raises(ServiceError): resolve_output_cap(question,Settings(_env_file=None,max_output_tokens=256))
    messages=[HumanMessage(content='旧问题'),AIMessage(content='旧答复'),HumanMessage(content='它呢')]
    result=resolved_messages(messages,'它呢','订单1001物流呢')
    assert result[-1].content=='订单1001物流呢' and messages[-1].content=='它呢'
    assert result[:-1]==messages[:-1]


@pytest.mark.anyio
async def test_observers_are_request_local():
    import asyncio
    from retailq.gateway import OpenAIModelGateway
    gateway=OpenAIModelGateway(Settings(_env_file=None,openai_api_key='test',openai_model='test'))
    class Bound:
        async def ainvoke(self,messages,**kwargs):
            await asyncio.sleep(0)
            return AIMessage(content=json.dumps({'resolved_question':messages[-1].content}),response_metadata={'finish_reason':'stop'})
    gateway.model=type('Model',(),{'bind':lambda self,**kw:Bound()})()
    left=[];right=[]
    async def first(row):left.append(row)
    async def second(row):right.append(row)
    try:
        await asyncio.gather(gateway.resolve_query([HumanMessage(content='甲')],max_tokens=256,observer=first,call_id='left'),
                             gateway.resolve_query([HumanMessage(content='乙')],max_tokens=256,observer=second,call_id='right'))
        assert {r['call_id'] for r in left}=={'left'} and {r['call_id'] for r in right}=={'right'}
        assert left[-1]['value']=='甲' and right[-1]['value']=='乙'
    finally:await gateway.aclose()


@pytest.mark.anyio
async def test_complete_explicit_order_question_cannot_be_stylistically_rewritten():
    from retailq.workflow.query_processing import QuerySteps
    class Gateway:
        async def resolve_query(self,*args,**kwargs):return '查询订单1002的运输进度',None
    raw='还是查询订单1002的运输进度'
    patch=await QuerySteps(Gateway(),Settings(_env_file=None)).resolve({'raw_question':raw,'messages':[HumanMessage(content=raw)]})
    assert patch['resolved_question']==raw


@pytest.mark.anyio
async def test_no_tool_control_decision_forces_json():
    from retailq.gateway import OpenAIModelGateway
    gateway=OpenAIModelGateway(Settings(_env_file=None,openai_api_key='test',openai_model='test'))
    bound=AsyncMock();bound.ainvoke.return_value=AIMessage(content='{"actions":[]}',response_metadata={'finish_reason':'stop'})
    from unittest.mock import Mock
    model=Mock();model.bind.return_value=bound;model.ainvoke=AsyncMock(return_value=bound.ainvoke.return_value)
    gateway.model=model
    try:
        assert (await gateway.decide_agent([HumanMessage(content='JSON控制决策')],[],max_tokens=128)).actions==()
        model.bind.assert_called_once_with(response_format={'type':'json_object'})
    finally:await gateway.aclose()
