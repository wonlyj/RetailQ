import pytest
from langchain_core.messages import HumanMessage
from retailq.config import Settings
from retailq.errors import ServiceError
from retailq.workflow.contracts import UsageCharge


@pytest.mark.parametrize('intent,route',[('物流','business'),('订单','business'),('售后','business'),
    ('商品咨询','knowledge'),('退款退货','knowledge'),('投诉','complaint'),('闲聊','smalltalk')])
def test_seven_intents_have_fixed_code_routes(intent,route):
    from retailq.workflow.classification import parse_intent,route_intent,resolve_reference
    assert route_intent(parse_intent('{"intent":"'+intent+'"}')) == route
    assert resolve_reference('它到了吗') == '它到了吗'


@pytest.mark.parametrize('text',['{"intent":"退款"}', '{"intent":"闲聊","intent":"投诉"}',
    '{"intent":"闲聊","route":"business"}','["投诉"]','```json\n{"intent":"闲聊"}\n```','{"intent":'])
def test_invalid_intent_is_error_not_business_fallback(text):
    from retailq.workflow.classification import parse_intent
    with pytest.raises(ServiceError) as error: parse_intent(text)
    assert error.value.code == 'intent_output_invalid'


@pytest.mark.anyio
async def test_classifier_uses_one_model_request_and_charges_shared_budget():
    from retailq.workflow.classification import IntentClassifier
    class Gateway:
        async def classify_intent(self,messages,*,max_tokens):
            assert max_tokens == 256 and [m for m in messages if m.type=='human'][-1].content == '你好'
            return '闲聊', UsageCharge(25,10,'provider')
    classifier = IntentClassifier(Gateway(),Settings(_env_file=None))
    result = await classifier.classify({'messages':[HumanMessage(content='你好')],'raw_question':'你好'})
    assert result['intent']=='闲聊' and result['route']=='smalltalk'
    assert result['agent']['usage']['entries'][0]['input_tokens'] == 25


@pytest.mark.anyio
async def test_no_budget_skips_even_classification_request():
    from retailq.workflow.classification import IntentClassifier
    class Gateway:
        async def classify_intent(self,*args,**kwargs): pytest.fail('budget must stop network call')
    with pytest.raises(ServiceError) as error:
        await IntentClassifier(Gateway(),Settings(_env_file=None,workflow_max_total_tokens=1)).classify(
            {'messages':[HumanMessage(content='你好')],'raw_question':'你好'})
    assert error.value.code=='workflow_budget_exceeded'


@pytest.mark.anyio
async def test_followup_classification_closes_context_with_current_role_instruction():
    from langchain_core.messages import AIMessage,SystemMessage
    from retailq.workflow.classification import IntentClassifier
    class Gateway:
        async def classify_intent(self,messages,*,max_tokens):
            assert isinstance(messages[-1],SystemMessage)
            assert '只输出' in messages[-1].content and 'intent' in messages[-1].content
            assert any(m.type=='ai' and 'T_DEMO' in m.content for m in messages)
            return '售后',None
    result=await IntentClassifier(Gateway(),Settings(_env_file=None)).classify({'raw_question':'刚才工单编号是什么？',
        'messages':[AIMessage(content='已确认工单T_DEMO'),HumanMessage(content='刚才工单编号是什么？')]})
    assert result['route']=='business'


@pytest.mark.anyio
@pytest.mark.parametrize('payload,valid',[('{"intent":"投诉"}',True),
    ('{"intent":"投诉","extra":1}',False), ('{"intent":"投诉","intent":"闲聊"}',False)])
async def test_real_gateway_intent_wire_requires_strict_json(payload,valid):
    from retailq.gateway import OpenAIModelGateway
    from tests.support.servers import run_server
    from tests.support.workflow_upstream import WorkflowUpstream
    peer = WorkflowUpstream('zero',bad_actions=payload)
    with run_server(peer.app) as url:
        gateway = OpenAIModelGateway(Settings(_env_file=None,openai_base_url=url,openai_api_key='test',openai_model='test'))
        try:
            if valid:
                intent,charge = await gateway.classify_intent([HumanMessage(content='我要投诉')],max_tokens=256)
                assert intent=='投诉' and charge.input_tokens==50
            else:
                with pytest.raises(ServiceError):
                    await gateway.classify_intent([HumanMessage(content='我要投诉')],max_tokens=256)
            assert len(peer.requests)==1 and 'tools' not in peer.requests[0]
            assert peer.requests[0].get('max_tokens',peer.requests[0].get('max_completion_tokens'))==256
        finally: await gateway.aclose()
