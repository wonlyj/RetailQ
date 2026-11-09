import time
from types import SimpleNamespace
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from retailq.workflow.contracts import TurnRef


@pytest.mark.anyio
@pytest.mark.parametrize('core_case',[True,False])
async def test_order_selection_resumes_same_turn_without_reclassification(tmp_path,core_case):
    from retailq.workflow.graph import build_graph
    from retailq.workflow.order_steps import OrderSteps
    from retailq.workflow.demo_orders import DemoOrderProvider
    seen=[]
    class Query:
        async def resolve(self,s):seen.append('resolve');return {'resolved_question':s['raw_question']}
        async def plan(self,s):seen.append('plan');return {'query_plan':{'core_case':core_case},'queries':[s['resolved_question']],'policy_mode':'policy_multi' if core_case else 'policy_single'}
        async def expand(self,s):seen.append('expand');return {'queries':[s['resolved_question'],'条件','流程']}
    class Classifier:
        async def classify(self,s):seen.append('intent');return {'intent':'退款退货','route':'core'}
    class Orders(OrderSteps):
        async def load_order(self,s):seen.append('load_order');return {'order_snapshot':self.provider.get_order(s['user_id'],s['order_id'])}
    class Evidence:
        async def retrieve_policy(self,s):seen.append('retrieve_policy');return {}
        async def gate(self,s):return {'gate':{'allowed':True}}
    class Agent:
        async def decide(self,s):seen.append('agent');return {'status':'answer','actions':[]}
        async def answer(self,s):return {'status':'completed','answer':'演示答复','actions':[]}
    class Repository:
        def finalize(self,*a):return SimpleNamespace(message_id=99)
        def cancel_pending(self,*a):pass
    state={'turn_id':'a'*32,'conversation_id':1,'user_message_id':1,'user_id':'alice',
           'raw_question':'这个能退吗','messages':[HumanMessage(content='这个能退吗')],'trace':[]}
    config={'configurable':{'thread_id':'selector'}}
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'pause.sqlite')) as saver:
        graph=build_graph(Agent(),Classifier(),Evidence(),Repository(),saver,query_steps=Query(),
            order_steps=Orders(DemoOrderProvider(),None,None,TurnRef('a'*32,1,1),deadline=time.monotonic()+30))
        await graph.ainvoke(state,config,durability='sync')
        paused=await graph.aget_state(config)
        assert paused.interrupts and seen==['resolve','intent']
        assert 0<paused.interrupts[0].value['pause_budget']['remaining_active_seconds']<=30
        await graph.ainvoke(Command(resume={'order_id':'1001'}),config,durability='sync')
        result=await graph.aget_state(config)
        assert not result.next and result.values['turn_id']=='a'*32
        assert seen==['resolve','intent','load_order','plan',*(['expand'] if core_case else []),'retrieve_policy','agent']


@pytest.mark.anyio
async def test_cancel_ends_graph_without_final_answer(tmp_path):
    # Reuses the actual order selector with an isolated minimal graph.
    from langgraph.graph import StateGraph,START,END
    from retailq.workflow.contracts import WorkflowState
    from retailq.workflow.order_steps import OrderSteps
    from retailq.workflow.demo_orders import DemoOrderProvider
    order=OrderSteps(DemoOrderProvider(),None,None,TurnRef('b'*32,2,2),deadline=time.monotonic()+20)
    builder=StateGraph(WorkflowState);builder.add_node('select',order.select_order)
    builder.add_edge(START,'select');builder.add_edge('select',END)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'cancel.sqlite')) as saver:
        graph=builder.compile(checkpointer=saver);cfg={'configurable':{'thread_id':'cancel'}}
        await graph.ainvoke({'turn_id':'b'*32,'conversation_id':2,'user_id':'alice','order_options':list(order.provider.list_orders('alice'))},cfg)
        result=await graph.ainvoke(Command(resume={'cancel':True}),cfg)
        assert result['status']=='cancelled' and not result.get('final_message_id')


@pytest.mark.anyio
@pytest.mark.parametrize('route',['core','other'])
async def test_core_and_other_deny_injected_tool_calls(route):
    from retailq.config import Settings
    from retailq.tools.runtime import ToolContext,build_registry
    from retailq.workflow.agent import AgentSteps
    from retailq.workflow.contracts import AgentDecision
    from retailq.errors import ServiceError
    from langchain_core.messages import AIMessage
    class Gateway:
        async def decide_agent(self,messages,tools,*,max_tokens):
            assert tools==[]
            return AgentDecision(AIMessage(content='',tool_calls=[{'id':'injected','name':'query_order','args':{'order_id':'1001'}}]),())
    settings=Settings(_env_file=None)
    agent=AgentSteps(Gateway(),build_registry(None,ToolContext(1,'alice'),settings),settings,lambda event:None)
    with pytest.raises(ServiceError) as error:
        await agent.decide({'route':route,'messages':[HumanMessage(content='问题')]})
    assert error.value.code=='agent_tool_denied'
