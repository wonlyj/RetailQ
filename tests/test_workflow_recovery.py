import pytest
from fastapi.testclient import TestClient
from retailq.app import create_app
from tests.support.database import chat_settings
from tests.support.servers import run_server
from tests.support.workflow_upstream import WorkflowUpstream
from tests.test_chat_api import parse_sse

pytestmark=pytest.mark.mysql


def test_sql_orphan_without_checkpoint_is_reconciled_on_next_turn(test_database,tmp_path):
    from retailq.schemas import ChatRequest
    peer=WorkflowUpstream('zero');peer.intent='闲聊'
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,workflow_checkpoint_path=str(tmp_path/'orphan.sqlite')))) as client:
        repo=client.app.state.workflow_repository
        orphan=repo.start_turn(ChatRequest(user_id='alice',message='在首次checkpoint前进程退出'))
        response=client.post('/api/chat/stream',json={'user_id':'alice','conversation_id':orphan.conversation_id,'message':'你好'})
        assert parse_sse(response.text)[-1][0]=='done'
        assert repo.get_turn(orphan.turn_id,'alice')['status']=='interrupted'


def test_prepare_failure_and_unconsumed_turn_receive_sql_terminal_state(test_database,tmp_path,monkeypatch):
    from retailq.schemas import ChatRequest
    from retailq.errors import ServiceError
    from retailq.workflow.models import WorkflowTurn
    from sqlalchemy import select
    peer=WorkflowUpstream('zero')
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,workflow_checkpoint_path=str(tmp_path/'prep.sqlite')))) as client:
        service=client.app.state.chat_service
        original=service.runtime.graph_for
        def fail(turn):raise ServiceError('checkpoint_read_failed',503,'fixture')
        monkeypatch.setattr(service.runtime,'graph_for',fail)
        assert client.post('/api/chat/stream',json={'user_id':'alice','message':'首次准备失败'}).status_code==503
        with test_database.sessions() as session:
            row=session.scalar(select(WorkflowTurn).where(WorkflowTurn.status=='failed'))
            assert row is not None and row.payload['error_code']=='checkpoint_read_failed'
        monkeypatch.setattr(service.runtime,'graph_for',original)
        prepared=client.portal.call(service.prepare,ChatRequest(user_id='alice',message='未开始消费流'))
        client.portal.call(service.release,prepared)
        assert service.workflow_repository.get_turn(prepared.ref.turn_id,'alice')['status']=='interrupted'


def test_file_reopen_continues_same_conversation_without_duplicate_history(test_database,tmp_path):
    peer=WorkflowUpstream('zero');peer.intent='闲聊'
    path=tmp_path/'persist.sqlite'
    with run_server(peer.app) as url:
        settings=chat_settings(url,workflow_checkpoint_path=str(path))
        with TestClient(create_app(settings)) as client:
            first=parse_sse(client.post('/api/chat/stream',json={'user_id':'alice','message':'你好'}).text)
            cid=first[0][1]['conversation_id']
            assert first[-1][0]=='done'
            assert path.is_file()
        peer.intent='订单'
        with TestClient(create_app(settings)) as client:
            second=parse_sse(client.post('/api/chat/stream',json={'user_id':'alice','message':'订单1001','conversation_id':cid}).text)
            assert second[-1][0]=='done'
            request=next(b for b in peer.requests if 'tools' in b)
            assert sum(m.get('content')=='你好' for m in request['messages'])==1
            assert sum(m.get('content')=='订单1001' for m in request['messages'])==1


@pytest.mark.anyio
async def test_graph_state_is_readable_after_async_sqlite_reopen(test_database,tmp_path):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from retailq.workflow.graph import build_graph
    from retailq.workflow.repository import WorkflowRepository
    from retailq.workflow.contracts import TurnRef
    # A completed fixed-route graph needs no agent or retriever implementation.
    class Classifier:
        async def classify(self,state): return {'intent':'闲聊','route':'smalltalk'}
    class Agent: settings=None
    class Queries:
        async def resolve(self,state):return {'resolved_question':state['raw_question']}
    repo=WorkflowRepository(test_database)
    from retailq.schemas import ChatRequest
    turn=repo.start_turn(ChatRequest(user_id='alice',message='你好'))
    config={'configurable':{'thread_id':'recovery-case'}}
    path=str(tmp_path/'state.sqlite')
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph=build_graph(Agent(),Classifier(),None,repo,saver,query_steps=Queries())
        state={'turn_id':turn.turn_id,'user_message_id':turn.user_message_id,'conversation_id':turn.conversation_id,
               'user_id':'alice','raw_question':'你好','messages':[],'trace':[]}
        await graph.ainvoke(state,config,durability='sync')
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph=build_graph(Agent(),Classifier(),None,repo,saver,query_steps=Queries())
        snapshot=await graph.aget_state(config)
        assert snapshot.values['raw_question']=='你好' and snapshot.next==()
        assert snapshot.values['final_message_id']>0


def test_identity_failure_releases_startup_lease(test_database,tmp_path):
    import json
    path=tmp_path/'bound.sqlite'
    peer=WorkflowUpstream('zero')
    with run_server(peer.app) as url:
        settings=chat_settings(url,workflow_checkpoint_path=str(path))
        with TestClient(create_app(settings)) as client:assert client.app.state.chat_service is not None
        binding=path.with_suffix('.sqlite.binding.json');original=binding.read_text(encoding='utf-8')
        binding.write_text('{"database":"foreign"}',encoding='utf-8')
        try:
            with TestClient(create_app(settings)) as client:
                assert client.post('/api/chat/stream',json={'message':'你好'}).status_code==409
        finally:binding.write_text(original,encoding='utf-8')
        with TestClient(create_app(settings)) as client:
            assert client.app.state.chat_service is not None


@pytest.mark.parametrize('phase',['after-tool','after-finalize'])
def test_process_exit_does_not_replay_committed_effects(test_database,tmp_path,phase):
    import json,os,subprocess,sys
    from sqlalchemy import select,func
    from retailq.models import Message
    from retailq.knowledge.rag_models import LowConfidenceQuestion
    path=tmp_path/'crash.sqlite';marker=tmp_path/'marker.json';report=tmp_path/'continued.json'
    env={**os.environ,'PYTHONIOENCODING':'utf-8'}
    args=[sys.executable,'-m','tests.support.workflow_worker','--checkpoint',str(path)]
    seed=subprocess.run([*args,'--phase',phase,'--output',str(marker)],env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert seed.returncode==0,seed.stdout+seed.stderr
    saved=json.loads(marker.read_text(encoding='utf-8'))
    continued=subprocess.run([*args,'--phase','continue','--cid',str(saved['cid']),'--output',str(report)],
                             env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert continued.returncode==0,continued.stdout+continued.stderr
    actual=json.loads(report.read_text(encoding='utf-8'))
    assert actual['done'] and actual['new_question_occurrences']==1
    with test_database.sessions() as session:
        users=session.scalar(select(func.count()).select_from(Message).where(Message.conversation_id==saved['cid'],Message.role=='user'))
        assert users==2
        if phase=='after-tool':
            assert session.scalar(select(func.count()).select_from(Message).where(Message.conversation_id==saved['cid'],Message.role=='tool'))==1
        else:
            assert session.scalar(select(func.count()).select_from(LowConfidenceQuestion).where(LowConfidenceQuestion.conversation_id==saved['cid']))==1
