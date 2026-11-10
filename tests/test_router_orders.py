import pytest
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from retailq.errors import ServiceError


def test_directory_and_query_share_stable_order_facts():
    from retailq.workflow.demo_orders import DemoOrderProvider
    provider=DemoOrderProvider()
    orders=provider.list_orders('alice')
    assert {o['order_id'] for o in orders}=={'1001','1002','001001'}
    for row in orders:
        assert row==provider.get_order('alice',row['order_id'])
        assert row['source']=='demo'
    assert provider.get_order('alice','1001')!=provider.get_order('alice','001001')
    with pytest.raises(ServiceError):provider.get_order('alice','9999')


def test_order_references_dont_guess_from_product_or_time():
    from retailq.workflow.order_steps import confirmed_order_ids
    assert confirmed_order_ids([], '商品2001用了7天')==()
    assert confirmed_order_ids([], '订单001001可以退吗')==('001001',)
    assert set(confirmed_order_ids([], '我有订单1001和1002'))=={'1001','1002'}
    assert confirmed_order_ids([AIMessage(content='也许您的订单是1001')], '它能退吗')==()


@pytest.mark.anyio
async def test_resolution_requires_picker_for_model_invented_id():
    from retailq.workflow.demo_orders import DemoOrderProvider
    from retailq.workflow.order_steps import OrderSteps
    steps=OrderSteps(DemoOrderProvider(),None,None,None)
    state={'user_id':'alice','raw_question':'商品2001能退吗','resolved_question':'订单1001能退吗',
           'messages':[HumanMessage(content='商品2001能退吗')]}
    result=await steps.resolve_order(state)
    assert result['order_id'] is None and len(result['order_options'])==3


@pytest.mark.anyio
async def test_multiple_historical_orders_require_selection():
    from retailq.workflow.demo_orders import DemoOrderProvider
    from retailq.workflow.order_steps import OrderSteps
    messages=[HumanMessage(content='订单1001和1002'),AIMessage(content='请选择'),HumanMessage(content='它能退吗')]
    result=await OrderSteps(DemoOrderProvider(),None,None,None).resolve_order({
        'user_id':'alice','raw_question':'它能退吗','resolved_question':'它能退吗','messages':messages})
    assert result['order_id'] is None


@pytest.mark.anyio
@pytest.mark.parametrize('raw,history',[
    ('不是订单1001，我要退另一单',[]),
    ('不要刚才那单，另一个能退吗',['订单1001的物流']),
    ('商品2001能退吗',['订单1001的物流']),
    ('我说的是另一个订单，不是之前那笔',['订单1001的物流']),
])
async def test_denied_or_changed_target_never_reuses_old_order(raw,history):
    from retailq.workflow.demo_orders import DemoOrderProvider
    from retailq.workflow.order_steps import OrderSteps
    messages=[*[HumanMessage(content=t) for t in history],HumanMessage(content=raw)]
    result=await OrderSteps(DemoOrderProvider(),None,None,None).resolve_order({
        'user_id':'alice','raw_question':raw,'resolved_question':raw,'messages':messages})
    assert result['order_id'] is None and len(result['order_options'])==3
    if raw.startswith('我说的是另一个'):
        changed='我要退新买的运动鞋，之前那单不要退'
        result=await OrderSteps(DemoOrderProvider(),None,None,None).resolve_order({
            'user_id':'alice','raw_question':changed,'resolved_question':changed,
            'messages':[HumanMessage(content='订单1001的物流'),HumanMessage(content=changed)]})
        assert result['order_id'] is None and len(result['order_options'])==3


@pytest.mark.anyio
@pytest.mark.mysql
async def test_replayed_order_node_reuses_receipt(test_database):
    from retailq.workflow.demo_orders import DemoOrderProvider
    from retailq.workflow.order_steps import OrderSteps
    from retailq.workflow.repository import WorkflowRepository
    from retailq.tools.runtime import ToolContext,build_registry
    from retailq.config import Settings
    from retailq.schemas import ChatRequest
    repo=WorkflowRepository(test_database)
    turn=repo.start_turn(ChatRequest(user_id='alice',message='订单1001能退吗'))
    registry=build_registry(None,ToolContext(turn.conversation_id,'alice'),Settings(_env_file=None))
    steps=OrderSteps(DemoOrderProvider(),registry,repo,turn)
    state={'order_id':'1001','user_id':'alice','messages':[HumanMessage(content='订单1001能退吗')]}
    result=await steps.load_order(state)
    async def forbidden(call):pytest.fail('committed receipt must not be re-executed')
    registry.execute=forbidden
    replay=await steps.load_order(state)
    assert replay['order_snapshot']==result['order_snapshot']
