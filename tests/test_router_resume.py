import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from retailq.app import create_app
from tests.support.database import chat_settings
from tests.support.servers import run_server
from tests.support.workflow_upstream import WorkflowUpstream
from tests.test_chat_api import parse_sse
from tests.test_rag_answer import retrieval

pytestmark=pytest.mark.mysql


def test_missing_order_pause_owned_and_cancelled(test_database,tmp_path):
    peer=WorkflowUpstream('zero');peer.intent='退款退货'
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,context_window_tokens=20000,workflow_checkpoint_path=str(tmp_path/'pause.sqlite')))) as client:
        events=parse_sse(client.post('/api/chat/stream',json={'message':'这个能退吗','user_id':'alice'}).text)
        frames=dict(events)
        assert 'order_selector' in frames and 'paused' in frames and 'done' not in frames and 'error' not in frames
        cid=frames['meta']['conversation_id'];turn=frames['meta']['turn_id'];card=frames['order_selector']
        assert 'pause_budget' not in card
        assert client.get(f'/api/conversations/{cid}/pending?user_id=alice').json()['pending']['interrupt_id']==card['interrupt_id']
        assert client.get(f'/api/conversations/{cid}/pending?user_id=bob').status_code==404
        assert client.post('/api/chat/stream',json={'message':'查物流','user_id':'alice','conversation_id':cid}).status_code==409
        endpoint=f'/api/conversations/{cid}/turns/{turn}/resume'
        assert client.post(endpoint,json={'user_id':'alice','interrupt_id':card['interrupt_id'],'request_key':'a'*32,'order_id':'9999'}).status_code==409
        cancelled=parse_sse(client.post(endpoint,json={'user_id':'alice','interrupt_id':card['interrupt_id'],'request_key':'b'*32,'cancel':True}).text)
        assert 'cancelled' in dict(cancelled) and 'done' not in dict(cancelled)
        assert client.get(f'/api/conversations/{cid}/pending?user_id=alice').json()=={'pending':None}


def test_resume_same_turn_and_completed_retry_has_no_second_model_or_user(test_database,tmp_path):
    from retailq.knowledge.workflow_policy import workflow_policy_identity
    from sqlalchemy import select,func
    from retailq.models import Message
    peer=WorkflowUpstream('zero');peer.intent='退款退货';peer.answer_chunks=['订单已查，具体按政策条件申请[1]。']
    policy_path=tmp_path/'policy.json'
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,context_window_tokens=20000,
            workflow_checkpoint_path=str(tmp_path/'resume.sqlite'),workflow_policy_multi_confidence_path=str(policy_path)))) as client:
        service=client.app.state.chat_service
        result=retrieval(2)
        from types import SimpleNamespace
        def policies(question,*,queries,mode,deadline):
            return SimpleNamespace(**{**vars(result),'strategy':mode})
        service.repository.policy_retrieve=policies
        identity=workflow_policy_identity(service.settings,result.snapshot,'policy_multi')
        policy_path.write_text(json.dumps({'identity':identity,'calibrated':True,'thresholds':{'policy_multi':0.0}}),encoding='utf-8')
        frames=dict(parse_sse(client.post('/api/chat/stream',json={'message':'我要退款','user_id':'alice'}).text))
        cid=frames['meta']['conversation_id'];turn=frames['meta']['turn_id'];card=frames['order_selector']
        body={'user_id':'alice','interrupt_id':card['interrupt_id'],'request_key':'c'*32,'order_id':'1001'}
        endpoint=f'/api/conversations/{cid}/turns/{turn}/resume'
        response=client.post(endpoint,json=body)
        resumed=dict(parse_sse(response.text))
        assert 'done' in resumed and 'error' not in resumed and resumed['meta']['turn_id']==turn
        calls=len(peer.requests)
        replay=dict(parse_sse(client.post(endpoint,json=body).text))
        assert replay['meta']['replayed'] and 'done' in replay and len(peer.requests)==calls
        assert client.post(endpoint,json={**body,'order_id':'1002'}).status_code==409
        with test_database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(Message).where(Message.conversation_id==cid,Message.role=='user'))==1


def test_cancel_is_available_even_when_business_budget_exhausted(test_database,tmp_path):
    from retailq.workflow.models import WorkflowTurn
    peer=WorkflowUpstream('zero');peer.intent='退款退货'
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,context_window_tokens=20000,workflow_checkpoint_path=str(tmp_path/'zero.sqlite')))) as client:
        frames=dict(parse_sse(client.post('/api/chat/stream',json={'message':'我要退货','user_id':'alice'}).text))
        cid=frames['meta']['conversation_id'];turn=frames['meta']['turn_id'];card=frames['order_selector']
        with test_database.sessions.begin() as session:
            row=session.get(WorkflowTurn,turn);row.payload={**row.payload,'remaining_active_seconds':0}
        response=client.post(f'/api/conversations/{cid}/turns/{turn}/resume',json={
            'user_id':'alice','interrupt_id':card['interrupt_id'],'request_key':'d'*32,'cancel':True})
        assert response.status_code==200 and 'cancelled' in dict(parse_sse(response.text))


def test_crash_before_sql_pause_preserves_spent_budget(test_database,tmp_path):
    import os,subprocess,sys
    marker=tmp_path/'marker.json';checkpoint=tmp_path/'crash.sqlite'
    command=[sys.executable,'-m','tests.support.router_worker','--checkpoint',str(checkpoint),'--marker',str(marker)]
    for phase in ('pause-crash','resume'):
        result=subprocess.run([*command,'--phase',phase],capture_output=True,text=True,encoding='utf-8',
            env={**os.environ,'PYTHONIOENCODING':'utf-8'},timeout=40)
        assert result.returncode==0,result.stdout+result.stderr
    actual=json.loads(marker.with_suffix('.resumed.json').read_text(encoding='utf-8'))
    assert actual['done'] and actual['remaining']<=30


def test_status_repairs_claim_before_checkpoint_consumption(test_database,tmp_path):
    from retailq.workflow.models import WorkflowTurn
    peer=WorkflowUpstream('zero');peer.intent='退款退货'
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,context_window_tokens=20000,workflow_checkpoint_path=str(tmp_path/'claim.sqlite')))) as client:
        frames=dict(parse_sse(client.post('/api/chat/stream',json={'message':'我要退货','user_id':'alice'}).text))
        cid=frames['meta']['conversation_id'];turn=frames['meta']['turn_id']
        with test_database.sessions.begin() as session:session.get(WorkflowTurn,turn).status='active'
        response=client.get(f'/api/conversations/{cid}/turns/{turn}?user_id=alice')
        assert response.json()['status']=='waiting_order'


def test_two_runtimes_serialize_same_resume(test_database,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from tests.support.router_policy import configure_policy
    entered=Event();release=Event()
    peer=WorkflowUpstream('zero');peer.intent='退款退货';peer.answer_chunks=['请依据政策核查[1]。']
    with run_server(peer.app) as url:
        settings=chat_settings(url,context_window_tokens=20000,workflow_checkpoint_path=str(tmp_path/'two.sqlite'))
        with TestClient(create_app(settings)) as first,TestClient(create_app(settings)) as second:
            for client in (first,second):configure_policy(client.app.state.chat_service.runtime,retrieval(2),tmp_path/'two-policy.json')
            original=first.app.state.chat_service.repository.policy_retrieve
            def slow(*args,**kwargs):
                entered.set()
                if not release.wait(10):raise RuntimeError('test must release retrieval')
                return original(*args,**kwargs)
            first.app.state.chat_service.repository.policy_retrieve=slow
            frames=dict(parse_sse(first.post('/api/chat/stream',json={'message':'我要退货','user_id':'alice'}).text))
            cid=frames['meta']['conversation_id'];turn=frames['meta']['turn_id'];selector=frames['order_selector']
            path=f'/api/conversations/{cid}/turns/{turn}/resume'
            body={'user_id':'alice','interrupt_id':selector['interrupt_id'],'request_key':'f'*32,'order_id':'1001'}
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(first.post,path,json=body)
                try:
                    assert entered.wait(8)
                    assert second.post(path,json=body).status_code==409
                finally:release.set()
                assert 'done' in dict(parse_sse(future.result(timeout=15).text))
            count=len(peer.requests)
            replay=dict(parse_sse(second.post(path,json=body).text))
            assert replay['meta']['replayed'] and len(peer.requests)==count
