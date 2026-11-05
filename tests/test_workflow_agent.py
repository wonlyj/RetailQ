import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from retailq.config import Settings
from retailq.gateway import OpenAIModelGateway
from retailq.tools.runtime import ToolContext, build_registry
from tests.support.servers import run_server
from tests.support.workflow_upstream import WorkflowUpstream

pytestmark = pytest.mark.anyio


async def run_case(peer, *, deadline=None, **overrides):
    from retailq.workflow.agent import AgentSteps, run_bare_agent
    with run_server(peer.app) as url:
        settings = Settings(_env_file=None, openai_base_url=url, openai_api_key='test',
                            openai_model='test', **overrides)
        gateway = OpenAIModelGateway(settings)
        events = []
        registry = build_registry(None, ToolContext(1,'alice'), settings)
        state = {'messages':[SystemMessage(content='Use tools, then output {"actions":[]}.'),
                             HumanMessage(content='先查订单1001，再查物流，两个都需要。')],
                 'raw_question':'先查订单1001，再查物流，两个都需要。'}
        try:
            result = await run_bare_agent(state, AgentSteps(gateway, registry, settings, events.append,deadline=deadline))
            return result, events
        finally:
            await gateway.aclose()


@pytest.mark.parametrize('scenario,names,calls', [('zero',[],2),('one',['query_logistics'],3),
    ('two',['query_order','query_logistics'],4)])
async def test_bare_agent_executes_until_tool_results_allow_convergence(scenario,names,calls):
    peer = WorkflowUpstream(scenario)
    state, events = await run_case(peer)
    assert [e.data['name'] for e in events if e.event=='tool_status' and e.data['status']=='completed'] == names
    assert state['agent']['model_calls'] == calls
    assert ''.join(e.data['text'] for e in events if e.event=='token') == '订单1001：以下为演示数据。'
    assert state['finish_reason'] == 'stop' and state['actions'] == []
    assert all(not t['function']['name'] in ('create_ticket','query_faq')
               for body in peer.requests for t in body.get('tools',[]))
    assert len([e for e in events if e.event=='token']) == 2


@pytest.mark.parametrize('scenario,code', [('create_ticket','agent_tool_denied'),('query_faq','agent_tool_denied'),
                                         ('parallel','multiple_tool_calls')])
async def test_unsafe_requests_never_execute(scenario,code):
    from retailq.errors import ServiceError
    with pytest.raises(ServiceError) as error:
        await run_case(WorkflowUpstream(scenario))
    assert error.value.code == code


@pytest.mark.parametrize('payload',['{"actions":["delete"]}', '{"actions":["ticket","ticket"]}',
    '{"actions":[],"answer":"hello"}', '{"actions":[],"actions":["ticket"]}', 'not json'])
async def test_invalid_control_json_cannot_create_actions(payload):
    from retailq.errors import ServiceError
    with pytest.raises(ServiceError) as error:
        await run_case(WorkflowUpstream('zero',bad_actions=payload))
    assert error.value.code == 'agent_control_invalid'


async def test_repeated_identical_tool_request_stops_without_unbounded_calls():
    peer = WorkflowUpstream('repeat')
    state, events = await run_case(peer)
    assert state['status'] == 'limited' and state['agent']['step'] == 1
    assert len(peer.requests) == 2
    assert '上限' in state['answer'] or '重复' in state['answer']


async def test_tool_limit_does_not_make_an_extra_final_model_call():
    peer = WorkflowUpstream('two')
    state, events = await run_case(peer, workflow_max_tool_steps=1)
    assert len(peer.requests) == 1 and state['status'] == 'limited'
    assert state['agent']['step'] == 1


async def test_exhausted_budget_never_contacts_upstream():
    peer = WorkflowUpstream('two')
    state, events = await run_case(peer, workflow_max_total_tokens=1)
    assert not peer.requests and state['status'] == 'limited'


async def test_length_stream_is_never_a_complete_answer():
    state, events = await run_case(WorkflowUpstream('zero',final_reason='length'))
    assert state['finish_reason'] == 'length' and state['status'] == 'interrupted'


async def test_model_call_limit_reserves_no_extra_final_generation():
    peer = WorkflowUpstream('zero')
    state, _ = await run_case(peer,workflow_max_model_calls=1)
    assert len(peer.requests) == 1 and state['status'] == 'limited'


async def test_elapsed_deadline_never_starts_a_model_call():
    peer = WorkflowUpstream('zero')
    state, _ = await run_case(peer,deadline=0)
    assert not peer.requests and state['status'] == 'limited'


async def test_oversize_tool_result_stops_with_paired_receipt():
    from langchain_core.tools import tool
    from retailq.tools.schemas import OrderInput
    from retailq.workflow.agent import AgentSteps,run_bare_agent
    @tool(args_schema=OrderInput)
    async def query_order(order_id: str) -> dict:
        """Controlled oversized business response."""
        return {'order_id':order_id,'text':'x'*20000}
    peer = WorkflowUpstream('two')
    with run_server(peer.app) as url:
        settings = Settings(_env_file=None,openai_base_url=url,openai_api_key='test',openai_model='test')
        registry = build_registry(None,ToolContext(1,'alice'),settings)
        registry._tools['query_order'] = query_order, True
        gateway = OpenAIModelGateway(settings)
        try:
            state = await run_bare_agent({'messages':[HumanMessage(content='订单1001')], 'raw_question':'订单1001'},
                AgentSteps(gateway,registry,settings,lambda event: None))
            assert state['status'] == 'limited' and len(peer.requests) == 1
            assert state['messages'][-2].tool_calls[0]['id'] == state['messages'][-1].tool_call_id
        finally: await gateway.aclose()
