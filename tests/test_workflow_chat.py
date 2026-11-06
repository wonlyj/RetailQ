from sqlalchemy import select,func
from fastapi.testclient import TestClient
import pytest
from retailq.app import create_app
from retailq.models import Ticket,Message
from retailq.knowledge.rag_models import LowConfidenceQuestion
from tests.support.database import chat_settings
from tests.support.servers import run_server
from tests.support.workflow_upstream import WorkflowUpstream
from tests.test_chat_api import parse_sse

pytestmark=pytest.mark.mysql


def test_local_readonly_identity_exposes_only_database_digest(test_database,tmp_path):
    peer=WorkflowUpstream('zero')
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,workflow_checkpoint_path=str(tmp_path/'identity.sqlite'))),
        base_url='http://127.0.0.1',client=('127.0.0.1',50000)) as client:
        response=client.get('/api/workflow/identity')
        assert response.status_code==200
        assert response.json()=={'database_identity':client.app.state.chat_service.runtime.identity}


def test_business_chat_uses_graph_tools_then_real_stream(test_database,tmp_path):
    peer=WorkflowUpstream('two')
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,workflow_checkpoint_path=str(tmp_path/'state.sqlite')))) as client:
        events=parse_sse(client.post('/api/chat/stream',json={'user_id':'alice','message':'先查订单1001再查物流'}).text)
        assert events[-1]==('done',{'finish_reason':'stop'})
        nodes=[e[1]['node'] for e in events if e[0]=='workflow' and e[1]['phase']=='enter']
        assert nodes[:3]==['resolve','intent','route'] and nodes[-1]=='log'
        assert 'retrieve' not in nodes and 'gate' not in nodes
        assert nodes.count('agent_decide')==3
        assert [e[1]['name'] for e in events if e[0]=='tool_status' and e[1]['status']=='completed']==['query_order','query_logistics']
        assert len([e for e in events if e[0]=='token'])==2
        cid=events[0][1]['conversation_id']
        assert client.post('/api/chat/stream',json={'user_id':'bob','conversation_id':cid,'message':'偷看'}).status_code==404
        assert len(peer.requests)==5  # classifier + three decisions + final stream


@pytest.mark.parametrize('intent,actions',[('投诉',['handoff','ticket']),('闲聊',[])])
def test_fixed_routes_have_one_classifier_and_no_agent_or_side_effects(test_database,tmp_path,intent,actions):
    peer=WorkflowUpstream('zero');peer.intent=intent
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,workflow_checkpoint_path=str(tmp_path/'state.sqlite')))) as client:
        events=parse_sse(client.post('/api/chat/stream',json={'user_id':'alice','message':'我要投诉' if intent=='投诉' else '你好'}).text)
        assert events[-1][0]=='done'
        assert len(peer.requests)==1
        frame=next((data for kind,data in events if kind=='actions'),{'actions':[]})
        assert sorted(a['kind'] for a in frame['actions'])==sorted(actions)
        assert not any(kind=='tool_status' for kind,data in events)
        with test_database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(Ticket).where(Ticket.conversation_id==events[0][1]['conversation_id']))==0


@pytest.mark.parametrize('intent',['商品咨询','退款退货'])
def test_knowledge_route_always_retrieves_before_gate_and_agent(test_database,tmp_path,intent):
    from tests.test_rag_answer import retrieval
    peer=WorkflowUpstream('zero');peer.intent=intent
    class Policy:
        def allows(self,result): return False
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,workflow_checkpoint_path=str(tmp_path/'state.sqlite')))) as client:
        runtime=client.app.state.chat_service.runtime
        runtime.chat_repository.rag_retrieve=lambda question,**kwargs: retrieval(0)
        runtime.policy=Policy()
        events=parse_sse(client.post('/api/chat/stream',json={'user_id':'alice','message':'知识库没有的政策'}).text)
        nodes=[data['node'] for kind,data in events if kind=='workflow' and data['phase']=='enter']
        assert nodes==['resolve','intent','route','retrieve','gate','fallback','log']
        assert len(peer.requests)==1 and events[-1][0]=='done'
        with test_database.sessions() as session:
            pool=session.scalar(select(LowConfidenceQuestion))
            assert pool.raw_question=='知识库没有的政策'


@pytest.mark.parametrize('valid_refs',[True,False])
def test_refund_evidence_precedes_agent_order_tool_and_references_control_commit(test_database,tmp_path,valid_refs):
    from tests.test_rag_answer import retrieval
    peer=WorkflowUpstream('two');peer.intent='退款退货'
    peer.answer_chunks=['退货规则见[1]。' if valid_refs else '没有引用的政策事实。','订单1001为演示结果。']
    class Policy:
        def allows(self,result):return True
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,workflow_checkpoint_path=str(tmp_path/'state.sqlite')))) as client:
        runtime=client.app.state.chat_service.runtime
        runtime.chat_repository.rag_retrieve=lambda q,**kwargs:retrieval(1);runtime.policy=Policy()
        events=parse_sse(client.post('/api/chat/stream',json={'user_id':'alice','message':'订单1001退货，先政策再查订单'}).text)
        nodes=[d['node'] for k,d in events if k=='workflow' and d['phase']=='enter']
        assert nodes.index('retrieve')<nodes.index('gate')<nodes.index('agent_decide')<nodes.index('tools')
        assert any(k=='tool_status' and d['name']=='query_order' for k,d in events)
        assert events[-1][0]==('done' if valid_refs else 'error')
        if not valid_refs:
            assert not any(k in ('sources','actions','done') for k,d in events)
            cid=events[0][1]['conversation_id']
            with test_database.sessions() as session:
                assert not session.scalar(select(Message).where(Message.conversation_id==cid,
                    Message.role=='assistant',Message.content=='没有引用的政策事实。订单1001为演示结果。'))


def test_unwritable_sqlite_does_not_fall_back_to_old_chat(test_database,tmp_path):
    path=tmp_path/'directory.sqlite';path.mkdir()
    peer=WorkflowUpstream('zero')
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,workflow_checkpoint_path=str(path)))) as client:
        response=client.post('/api/chat/stream',json={'message':'你好'})
        assert response.status_code==503 and not peer.requests
        assert response.json()['error']['code']=='workflow_checkpoint_unavailable'


def test_missing_workflow_table_requires_explicit_migration(test_database,tmp_path):
    # Reversible rename of this isolated fixture's empty metadata table.
    with test_database.engine.begin() as connection:
        connection.exec_driver_sql('RENAME TABLE workflow_actions TO workflow_actions_ch05_missing')
    try:
        peer=WorkflowUpstream('zero')
        with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,workflow_checkpoint_path=str(tmp_path/'s.sqlite')))) as client:
            response=client.post('/api/chat/stream',json={'message':'你好'})
            assert response.status_code==503 and response.json()['error']['code']=='workflow_schema_missing'
            assert not peer.requests
    finally:
        with test_database.engine.begin() as connection:
            connection.exec_driver_sql('RENAME TABLE workflow_actions_ch05_missing TO workflow_actions')


@pytest.mark.anyio
async def test_same_database_lock_survives_knowledge_disabled_and_different_runtimes(test_database,tmp_path):
    from retailq.repositories import ChatRepository
    from retailq.workflow.repository import WorkflowRepository
    from retailq.workflow.runtime import WorkflowRuntime
    from retailq.workflow.service import WorkflowChatService
    from retailq.coordination import Coordinator
    from retailq.schemas import ChatRequest
    from retailq.errors import ServiceError
    settings=chat_settings('http://127.0.0.1:9',workflow_checkpoint_path=str(tmp_path/'shared.sqlite'))
    coordinator=Coordinator.for_settings(settings,bind_knowledge=False)
    repo=WorkflowRepository(test_database)
    async with WorkflowRuntime.open(settings,repo,ChatRepository(test_database),object(),None,None,coordinator) as first:
        async with WorkflowRuntime.open(settings,repo,ChatRepository(test_database),object(),None,None,coordinator) as second:
            a,b=WorkflowChatService(first),WorkflowChatService(second)
            turn=await a.prepare(ChatRequest(user_id='alice',message='订单1001'))
            other=None
            try:
                with pytest.raises(ServiceError) as error:
                    await b.prepare(ChatRequest(user_id='alice',message='同时请求',conversation_id=turn.conversation_id))
                assert error.value.status_code==409
                other=await b.prepare(ChatRequest(user_id='alice',message='另一会话'))
                assert other.conversation_id!=turn.conversation_id
            finally:
                await a.release(turn)
                if other:await b.release(other)
