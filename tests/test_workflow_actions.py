import asyncio
from threading import Event
import pytest
from sqlalchemy import select,func
from retailq.config import Settings
from retailq.coordination import Coordinator
from retailq.models import Ticket,Conversation
from retailq.repositories import ChatRepository
from retailq.schemas import ChatRequest
from retailq.errors import ServiceError
from retailq.workflow.repository import WorkflowRepository

pytestmark=[pytest.mark.mysql,pytest.mark.anyio]


def setup_action(db):
    from retailq.workflow.actions import TicketActionService
    settings=Settings(_env_file=None,mysql_port=3307,mysql_database='retailq_test',tool_timeout_seconds=.05)
    repo=WorkflowRepository(db)
    turn=repo.start_turn(ChatRequest(user_id='alice',message='我要投诉'))
    final=repo.finalize(turn,{'user_id':'alice','answer':'请选择','status':'completed','actions':['handoff','ticket']})
    action=next(a for a in final.actions if a['kind']=='ticket')
    coordinator=Coordinator.for_settings(settings,bind_knowledge=False)
    service=TicketActionService(repo,ChatRepository(db),coordinator,settings)
    args={'cid':turn.conversation_id,'user_id':'alice','action_id':action['action_id'],
          'description':'订单1001延迟，请核查','ticket_type':'投诉'}
    return service,repo,turn,args,final


async def test_confirm_twice_creates_one_ticket_without_handoff(test_database):
    service,repo,turn,args,final=setup_action(test_database)
    first=await service.confirm(**args);second=await service.confirm(**args)
    assert first.status==second.status=='completed'
    assert first.ticket['ticket_no']==second.ticket['ticket_no']
    with test_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Ticket).where(Ticket.conversation_id==turn.conversation_id))==1
        assert session.get(Conversation,turn.conversation_id).status=='进行中'
    with pytest.raises(ServiceError) as error:await service.confirm(**{**args,'description':'改变描述'})
    assert error.value.status_code==409
    with pytest.raises(ServiceError):await service.confirm(**{**args,'user_id':'bob'})
    handoff=next(a for a in final.actions if a['kind']=='handoff')
    with pytest.raises(ServiceError):await service.confirm(**{**args,'action_id':handoff['action_id']})
    await service.aclose()


async def test_suggestions_and_continuing_without_confirmation_have_no_side_effect(test_database):
    service,repo,turn,args,_=setup_action(test_database)
    next_turn=repo.start_turn(ChatRequest(user_id='alice',conversation_id=turn.conversation_id,message='先继续聊天'))
    assert (await service.get(turn.conversation_id,'alice',args['action_id'])).status=='offered'
    with test_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Ticket).where(Ticket.conversation_id==turn.conversation_id))==0


async def test_pending_write_holds_same_conversation_lease_until_drain(test_database,monkeypatch):
    service,repo,turn,args,_=setup_action(test_database)
    entered,release=Event(),Event()
    original=service.chat_repository.create_ticket
    def slow(*a,**kw):
        entered.set()
        if not release.wait(3):raise RuntimeError('test did not release')
        return original(*a,**kw)
    monkeypatch.setattr(service.chat_repository,'create_ticket',slow)
    try:
        first=await service.confirm(**args)
        assert entered.is_set() and first.status=='unknown'
        assert service.coordinator.try_acquire('conversation',str(turn.conversation_id)) is None
        another=service.coordinator.try_acquire('conversation',str(turn.conversation_id+1))
        assert another is not None;another.close()
        closing=asyncio.create_task(service.aclose());await asyncio.sleep(0)
        assert not closing.done()
        release.set();await closing
        result=await service.get(turn.conversation_id,'alice',args['action_id'])
        assert result.status=='completed' and result.ticket['ticket_no']
        lock=service.coordinator.try_acquire('conversation',str(turn.conversation_id))
        assert lock is not None;lock.close()
    finally:release.set();await service.aclose()


async def test_cancelled_confirmation_retains_background_operation_and_unique_fact(test_database,monkeypatch):
    service,repo,turn,args,_=setup_action(test_database)
    entered,release=Event(),Event();original=service.chat_repository.create_ticket
    def slow(*a,**kw):
        entered.set();release.wait(3);return original(*a,**kw)
    monkeypatch.setattr(service.chat_repository,'create_ticket',slow)
    task=asyncio.create_task(service.confirm(**args))
    try:
        assert await asyncio.to_thread(entered.wait,1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert service.coordinator.try_acquire('conversation',str(turn.conversation_id)) is None
        release.set();await service.aclose()
        assert (await service.get(turn.conversation_id,'alice',args['action_id'])).status=='completed'
        context=repo.load_projection(turn.conversation_id,'alice')
        assert sum('用户通过按钮确认' in m.content for t in context for m in t)==1
    finally:release.set();await service.aclose()


async def test_existing_executing_action_without_receipt_is_not_replayed(test_database):
    service,repo,turn,args,_=setup_action(test_database)
    repo.claim_action(**args)
    result=await service.confirm(**args)
    assert result.status=='unknown' and result.ticket is None
    with test_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Ticket).where(Ticket.conversation_id==turn.conversation_id))==0


async def test_committed_ticket_with_unsettled_action_recovers_from_persistent_receipt(test_database):
    service,repo,turn,args,_=setup_action(test_database)
    claim=repo.claim_action(**args)
    result=service.chat_repository.create_ticket(turn.conversation_id,'alice',args['description'],args['ticket_type'],claim['tool_call_id'])
    recovered=await service.get(turn.conversation_id,'alice',args['action_id'])
    assert recovered.status=='completed' and recovered.ticket==result
    assert (await service.confirm(**args)).ticket==result


def test_http_ticket_confirmation_is_distinct_from_handoff(test_database,tmp_path):
    from fastapi.testclient import TestClient
    from retailq.app import create_app
    from tests.support.database import chat_settings
    from tests.support.servers import run_server
    from tests.support.workflow_upstream import WorkflowUpstream
    from tests.test_chat_api import parse_sse
    peer=WorkflowUpstream('zero');peer.intent='投诉'
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,workflow_checkpoint_path=str(tmp_path/'s.sqlite')))) as client:
        events=parse_sse(client.post('/api/chat/stream',json={'user_id':'alice','message':'我要投诉'}).text)
        cid=events[0][1]['conversation_id']
        actions=next(d['actions'] for k,d in events if k=='actions')
        ticket=next(a for a in actions if a['kind']=='ticket')
        handoff=next(a for a in actions if a['kind']=='handoff')
        endpoint=f'/api/conversations/{cid}/actions/{ticket["action_id"]}/ticket'
        body={'user_id':'alice','description':'订单1001延迟','ticket_type':'投诉'}
        assert client.post(endpoint,json={**body,'description':''}).status_code==422
        assert client.post(endpoint,json={**body,'user_id':'bob'}).status_code==404
        assert client.post(f'/api/conversations/{cid}/actions/{handoff["action_id"]}/ticket',json=body).status_code==404
        first=client.post(endpoint,json=body)
        assert first.status_code==200 and first.json()['status']=='completed'
        assert client.post(endpoint,json=body).json()==first.json()
        assert client.get(endpoint,params={'user_id':'alice'}).json()==first.json()
        with test_database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(Ticket).where(Ticket.conversation_id==cid))==1
            assert session.get(Conversation,cid).status=='进行中'
