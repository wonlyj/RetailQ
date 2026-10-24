import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from retailq.app import create_app
from retailq.errors import ServiceError
from retailq.models import Ticket
from retailq.repositories import ChatRepository
from tests.support.database import chat_settings
from tests.support.servers import run_server
from tests.support.tool_upstream import ControlledToolUpstream, wire_call
from tests.test_chat_api import parse_sse

pytestmark = pytest.mark.mysql


@pytest.mark.anyio
async def test_script_client_accepts_tool_frames_and_server_id(test_database):
    from scripts.client import chat
    upstream = ControlledToolUpstream()
    with run_server(upstream.app) as url, run_server(create_app(chat_settings(url))) as api_url:
        async with httpx.AsyncClient(trust_env=False) as client:
            actual = await chat(client,api_url,'物流1001',[],user_id='alice')
            assert actual['conversation_id'] > 0
            assert actual['tool_status'][-1]['name'] == 'query_logistics'
            assert actual['tool_status'][-1]['status'] == 'completed'


def test_script_client_retains_public_database_error_code():
    from scripts.client import ApiFailure, check_response
    response = httpx.Response(503,json={'error':{'code':'database_not_configured','message':'configure database'}})
    with pytest.raises(ApiFailure) as error:
        check_response(response)
    assert error.value.code == 'database_not_configured'


def test_sse_logistics_persisted_roundtrip(test_database):
    upstream = ControlledToolUpstream()
    repo = ChatRepository(test_database)
    with run_server(upstream.app) as url, TestClient(create_app(chat_settings(url))) as client:
        response = client.post('/api/chat/stream',json={'message':'订单1001物流','user_id':'alice'})
        assert response.status_code == 200
        events = parse_sse(response.text)
        assert [e[0] for e in events] == ['meta','tool_status','tool_status','token','token','done']
        assert events[1][1]['status'] == 'running' and events[2][1]['status'] == 'completed'
        cid = events[0][1]['conversation_id']
        records = repo.load_messages(cid,'alice')
        assert [r.role for r in records] == ['user','assistant','tool','assistant']
        assert records[1].tool_calls[0]['id'] == records[2].tool_call_id == 'call_1'
        assert json.loads(records[2].content)['data']['order_id'] == '1001'
        upstream.calls = []
        upstream.selection_reason = 'stop'
        second = client.post('/api/chat/stream',json={'message':'刚才订单号','conversation_id':cid,'user_id':'alice'})
        assert parse_sse(second.text)[-1][0] == 'done'
        wire = upstream.requests[2][0]['messages']
        assert [m['role'] for m in wire] == ['system','user','assistant','tool','assistant','user']
        assert repo.load_messages(cid,'alice')[-1].role == 'assistant'
        assert client.post('/api/chat/stream',json={'message':'偷看','conversation_id':cid,'user_id':'bob'}).status_code == 404


def test_case_distinct_call_ids_keep_separate_receipts_and_create_ticket(test_database):
    upstream = ControlledToolUpstream([wire_call('query_order',{'order_id':'1001'},'call_1')])
    repo = ChatRepository(test_database)
    with run_server(upstream.app) as url, TestClient(create_app(chat_settings(url))) as client:
        first = parse_sse(client.post('/api/chat/stream',json={'message':'查订单1001','user_id':'alice'}).text)
        cid = first[0][1]['conversation_id']
        upstream.calls = [wire_call('create_ticket',{'description':'订单1001延迟','ticket_type':'投诉'},'CALL_1')]
        second = parse_sse(client.post('/api/chat/stream',json={'message':'请创建投诉工单','user_id':'alice','conversation_id':cid}).text)
        assert second[-1] == ('done',{'finish_reason':'stop'})
        receipts = {r.tool_call_id:json.loads(r.content) for r in repo.load_messages(cid,'alice') if r.role=='tool'}
        assert set(receipts) == {'call_1','CALL_1'}
        assert receipts['call_1']['data']['order_id'] == '1001'
        number = receipts['CALL_1']['data']['ticket_no']
        with test_database.sessions() as session:
            assert session.get(Ticket,number).conversation_id == cid
        # append_message's duplicate check must use the same exact comparison.
        repo.append_message(cid,'alice','tool','{"ok":false}',None,'CaLl_1')
        assert len([r for r in repo.load_messages(cid,'alice') if r.role=='tool'])==3


@pytest.mark.parametrize('reason',['content_filter','unexpected_reason'])
def test_abnormal_finish_is_error_and_never_persisted_as_answer(test_database,reason):
    upstream=ControlledToolUpstream(final_reason=reason)
    with run_server(upstream.app) as url,TestClient(create_app(chat_settings(url))) as client:
        events=parse_sse(client.post('/api/chat/stream',json={'message':'订单1001物流','user_id':'alice'}).text)
        assert events[-1][0]=='error'
        assert not any(name=='done' for name,_ in events)
        cid=events[0][1]['conversation_id']
        assert [r.role for r in ChatRepository(test_database).load_messages(cid,'alice')]==['user','assistant','tool']


@pytest.mark.parametrize('case',['multiple','invalid','unknown','no_tool','length','eof','second_tool','final_commit'])
def test_tool_failures_and_no_tool_contract(test_database, monkeypatch, case):
    kwargs = {}
    if case == 'multiple': kwargs['calls'] = [wire_call(),wire_call(call_id='call_2')]
    if case == 'invalid': kwargs['calls'] = [wire_call(args={'order_id':1001})]
    if case == 'unknown': kwargs['calls'] = [wire_call(name='not_registered')]
    if case == 'no_tool': kwargs['no_tool'] = True
    if case == 'length': kwargs['final_reason'] = 'length'
    if case == 'eof': kwargs['terminal'] = False
    if case == 'second_tool': kwargs['final_tool'] = True
    upstream = ControlledToolUpstream(**kwargs)
    with run_server(upstream.app) as url, TestClient(create_app(chat_settings(url))) as client:
        if case == 'final_commit':
            repo = client.app.state.chat_service.repository
            original = repo.append_message
            def fail(cid,user,role,content,*args,**kw):
                if role=='assistant' and content and not args:
                    raise ServiceError('database_unavailable',503,'数据库操作未确认成功。')
                return original(cid,user,role,content,*args,**kw)
            monkeypatch.setattr(repo,'append_message',fail)
        response = client.post('/api/chat/stream',json={'message':'物流1001','user_id':'alice'})
        events = parse_sse(response.text)
        assert response.status_code == 200
        if case in ['multiple','eof','second_tool','final_commit']:
            assert events[-1][0] == 'error'
            assert not any(e[0]=='done' for e in events)
        else:
            assert events[-1] == ('done',{'finish_reason':'length' if case=='length' else 'stop'})
        if case in ['invalid','unknown']:
            status = [d for e,d in events if e=='tool_status'][-1]
            assert status['status'] == 'failed'
            assert json.loads(upstream.requests[-1][0]['messages'][-1]['content'])['ok'] is False
        if case == 'no_tool':
            assert not any(e[0]=='tool_status' for e in events)
            assert len(upstream.requests)==2
        if case == 'multiple':
            with test_database.sessions() as session:
                assert session.scalar(select(func.count()).select_from(Ticket))==1
        assert 'password-secret' not in response.text


@pytest.mark.anyio
async def test_disconnect_after_ticket_commit_and_concurrent_409(test_database):
    upstream = ControlledToolUpstream([wire_call('create_ticket',{'description':'订单1001延迟','ticket_type':'投诉'})],gated=True)
    repo = ChatRepository(test_database)
    with run_server(upstream.app) as url, run_server(create_app(chat_settings(url))) as api_url:
        try:
            async with httpx.AsyncClient(trust_env=False,timeout=8) as client:
                async with client.stream('POST',api_url+'/api/chat/stream',json={'message':'订单1001请建投诉工单','user_id':'alice'}) as response:
                    block=[]
                    cid=None
                    async for line in response.aiter_lines():
                        if line: block.append(line)
                        else:
                            events=parse_sse('\n'.join(block)+'\n\n');block=[]
                            if not events: continue
                            name,data=events[0]
                            if name=='meta': cid=data['conversation_id']
                            if name=='tool_status' and data['status']=='completed':
                                concurrent=await client.post(api_url+'/api/chat/stream',json={'message':'同时请求','user_id':'alice','conversation_id':cid})
                                assert concurrent.status_code==409
                                break
                async with asyncio.timeout(3):
                    while True:
                        rows=await asyncio.to_thread(repo.load_messages,cid,'alice')
                        if any(r.role=='tool' for r in rows): break
                        await asyncio.sleep(0.01)
                assert [r.role for r in rows]==['user','assistant','tool']
                assert json.loads(rows[-1].content)['data']['ticket_no']
        finally:
            upstream.release.set()
