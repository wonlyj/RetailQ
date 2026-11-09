import asyncio
import time
from threading import Event
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage,HumanMessage


@pytest.mark.anyio
async def test_cancelled_graph_sql_keeps_lease_until_actual_thread_exit(tmp_path):
    from retailq.config import Settings
    from retailq.workflow.contracts import TurnRef,FinalizedTurn
    from retailq.workflow.graph import build_graph
    from langgraph.checkpoint.memory import MemorySaver
    from retailq.workflow.service import WorkflowChatService,PreparedWorkflowTurn
    entered,finish=Event(),Event()
    class Repository:
        def mark_terminal(self,*args):pass
        def finalize(self,*args):
            entered.set()
            assert finish.wait(5)
            return FinalizedTurn('a'*32,2,None,())
    class Classifier:
        async def classify(self,state):return {'intent':'闲聊','route':'smalltalk'}
    class Queries:
        async def resolve(self,state):return {'resolved_question':state['raw_question']}
    class Registry:
        pending=()
        async def drain(self):pass
    from retailq.coordination import Coordinator,RuntimeIdentity
    identity=RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',tmp_path/'corpus')
    coordinator=Coordinator(tmp_path/'locks',identity,bind_knowledge=False)
    other=Coordinator(tmp_path/'locks',identity,bind_knowledge=False)
    repo=Repository();lease=coordinator.try_acquire('conversation','1')
    runtime=SimpleNamespace(chat_repository=repo,repository=repo,gateway=None,settings=Settings(_env_file=None),coordinator=None)
    service=WorkflowChatService(runtime)
    turn=PreparedWorkflowTurn(TurnRef('a'*32,1,1),'alice','你好',None,Registry(),lease,time.monotonic()+10)
    turn.graph=build_graph(None,Classifier(),None,repo,MemorySaver(),query_steps=Queries())
    runtime.snapshot=lambda cid:turn.graph.aget_state({'configurable':{'thread_id':'cancel-sql'}})
    turn.state={'turn_id':'a'*32,'conversation_id':1,'user_message_id':1,'user_id':'alice','raw_question':'你好','trace':[]}
    async def consume():
        try:
            async for event in service._graph_events(turn,{'configurable':{'thread_id':'cancel-sql'}}):pass
        finally:await service.release(turn)
    task=asyncio.create_task(consume())
    try:
        reached=await asyncio.to_thread(entered.wait,2)
        if not reached and task.done():task.result()
        assert reached
        task.cancel();await asyncio.sleep(.04)
        competing=other.try_acquire('conversation','1')
        if competing is not None:competing.close()
        assert competing is None and not task.done()
    finally:
        finish.set()
        await asyncio.gather(task,return_exceptions=True)
    acquired=other.try_acquire('conversation','1')
    assert acquired is not None
    acquired.close()


@pytest.mark.anyio
@pytest.mark.parametrize('blocked',['request','receipt'])
async def test_cancelled_agent_sql_does_not_finish_before_its_thread(blocked):
    from retailq.config import Settings
    from retailq.tools.runtime import ToolContext,build_registry
    from retailq.workflow.agent import AgentSteps
    entered,finish=Event(),Event()
    class Repository:
        def write(self,kind):
            if kind==blocked:
                entered.set();assert finish.wait(5)
        def record_tool_request(self,*args):self.write('request')
        def record_tool_result(self,*args):self.write('receipt')
    settings=Settings(_env_file=None)
    steps=AgentSteps(None,build_registry(None,ToolContext(1,'alice'),settings),settings,lambda e:None,repository=Repository())
    selection=AIMessage(content='',tool_calls=[{'id':'call1','name':'query_order','args':{'order_id':'1001'}}])
    task=asyncio.create_task(steps.execute({'messages':[HumanMessage(content='订单1001')],
        'agent':{'pending_selection':selection.model_dump()}}))
    try:
        assert await asyncio.to_thread(entered.wait,2)
        task.cancel();await asyncio.sleep(.04)
        assert not task.done()
    finally:
        finish.set();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.anyio
async def test_cancel_during_new_turn_commit_keeps_ref_for_terminal_cleanup():
    from retailq.config import Settings
    from retailq.schemas import ChatRequest
    from retailq.workflow.contracts import TurnRef
    from retailq.workflow.service import WorkflowChatService
    entered,finish=Event(),Event();terminal=[]
    class Repository:
        def start_turn(self,body):
            entered.set();assert finish.wait(5)
            return TurnRef('e'*32,1,1)
        def mark_terminal(self,ref,status,code):terminal.append((ref.turn_id,status))
    repo=Repository();runtime=SimpleNamespace(repository=repo,chat_repository=repo,gateway=None,
        settings=Settings(_env_file=None),coordinator=None)
    service=WorkflowChatService(runtime)
    task=asyncio.create_task(service.prepare(ChatRequest(user_id='alice',message='hello')))
    try:
        assert await asyncio.to_thread(entered.wait,2)
        task.cancel();await asyncio.sleep(.02);finish.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert terminal==[('e'*32,'interrupted')]
    finally:
        finish.set();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize('knowledge',[False,True])
async def test_long_history_drops_old_turns_for_current_stage_and_evidence(knowledge):
    from retailq.config import Settings
    from retailq.schemas import ChatRequest
    from retailq.workflow.contracts import TurnRef,AgentDecision
    from retailq.workflow.service import WorkflowChatService
    from retailq.workflow.agent import AgentSteps,run_bare_agent
    from retailq.workflow.evidence import EvidenceSteps
    from retailq.gateway import ModelChunk
    from tests.test_rag_answer import retrieval
    class Repository:
        def interrupt_active(self,*args):pass
        def latest_turn(self,*args):return None
        def load_projection(self,*args):return [[HumanMessage(content='hello'),AIMessage(content='world')] for _ in range(300)]
        def start_turn(self,*args):return TurnRef('a'*32,1,1)
    class Gateway:
        async def decide_agent(self,*args,**kwargs):return AgentDecision(AIMessage(content='{"actions":[]}'),())
        async def stream_answer(self,*args,**kwargs):
            yield ModelChunk('已答[1]' if knowledge else '已答')
            yield ModelChunk(finish_reason='stop')
    class Runtime:
        repository=Repository();chat_repository=repository;gateway=Gateway();settings=Settings(_env_file=None)
        coordinator=SimpleNamespace(try_acquire=lambda *args:SimpleNamespace(close=lambda:None))
        def graph_for(self,turn):return None
        async def snapshot(self,cid):return SimpleNamespace(interrupts=(),values={},next=())
        async def rebase(self,*args):return {}
    service=WorkflowChatService(Runtime())
    turn=await service.prepare(ChatRequest(user_id='alice',conversation_id=1,message='当前短问题'))
    state={'raw_question':'当前短问题','messages':turn.prepared.messages}
    if knowledge:
        evidence=EvidenceSteps(lambda q:retrieval(1),SimpleNamespace(allows=lambda r:True),Runtime.settings)
        state={**state,**await evidence.retrieve(state)}
        state={**state,**await evidence.gate(state)}
    result=await run_bare_agent(state,AgentSteps(Runtime.gateway,turn.registry,Runtime.settings,lambda e:None))
    assert result['status']=='completed' and result['answer'].startswith('已答')


@pytest.mark.anyio
async def test_bare_agent_tool_is_bounded_by_remaining_turn_deadline():
    from retailq.config import Settings
    from retailq.workflow.agent import AgentSteps
    class Registry:
        tools=[];context=SimpleNamespace()
        async def execute(self,call):
            await asyncio.sleep(.15)
            return SimpleNamespace(payload={'ok':True,'data':{}},persistence='none')
    selection=AIMessage(content='',tool_calls=[{'id':'deadline','name':'query_order','args':{'order_id':'1001'}}])
    steps=AgentSteps(None,Registry(),Settings(_env_file=None),lambda e:None,deadline=time.monotonic()+.02)
    start=time.monotonic()
    result=await steps.execute({'messages':[],'agent':{'pending_selection':selection.model_dump()}})
    assert result['status']=='limited' and time.monotonic()-start<.12


def test_expired_turn_cannot_start_normalization_after_slow_snapshot():
    from contextlib import nullcontext
    from threading import BoundedSemaphore
    from retailq.config import Settings
    from retailq.errors import ServiceError
    from retailq.knowledge.runtime import KnowledgeRuntime
    runtime=KnowledgeRuntime.__new__(KnowledgeRuntime)
    runtime.settings=Settings(_env_file=None);runtime._read_slots=BoundedSemaphore(1)
    runtime._operation=lambda:nullcontext()
    def snapshot():time.sleep(.04);return SimpleNamespace()
    runtime.corpus=SimpleNamespace(active=snapshot)
    runtime.query_gateway=SimpleNamespace(normalize=lambda *args:pytest.fail('expired work must not start model request'))
    with pytest.raises(ServiceError) as error:runtime.retrieve('公开问题',deadline=time.monotonic()+.01)
    assert error.value.code=='knowledge_query_timeout'


@pytest.mark.anyio
async def test_evidence_propagates_absolute_turn_deadline_to_worker():
    from retailq.config import Settings
    from retailq.workflow.evidence import EvidenceSteps
    from tests.test_rag_answer import retrieval
    deadline=time.monotonic()+5
    def retrieve(question,*,deadline):
        assert deadline<=expected
        return retrieval(1)
    expected=deadline
    steps=EvidenceSteps(retrieve,None,Settings(_env_file=None),deadline=deadline)
    await steps.retrieve({'raw_question':'问题','messages':[HumanMessage(content='问题')]})


@pytest.mark.anyio
@pytest.mark.parametrize('fault',['missing','duplicate','wrong_turn','wrong_message','valid'])
async def test_python_stream_client_requires_bound_explicit_actions(fault):
    import httpx,json
    from scripts.client import chat,ApiFailure
    meta={'conversation_id':1,'turn_id':'a'*32}
    action={'conversation_id':1,'turn_id':'a'*32,'message_id':'2','actions':[]}
    frames=[('meta',meta),('token',{'text':'请选择'})]
    if fault=='wrong_turn':action['turn_id']='b'*32
    if fault=='wrong_message':frames.append(('sources',{'turn_id':'a'*32,'message_id':'3','citations':[]}))
    if fault!='missing':frames.append(('actions',action))
    if fault=='duplicate':frames.append(('actions',action))
    frames.append(('done',{'finish_reason':'stop'}))
    body=''.join('event: '+kind+'\ndata: '+json.dumps(data)+'\n\n' for kind,data in frames)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text=body,headers={'content-type':'text/event-stream'}))) as client:
        if fault=='valid':assert (await chat(client,'http://local','hello',[]))['actions']==[]
        else:
            with pytest.raises(ApiFailure):await chat(client,'http://local','hello',[])
