"""Real app/runtime/SQL/Lite; only external model providers are substituted."""
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient

from retailq.config import Settings
from retailq.knowledge.models import KnowledgeChunk
from tests.support.reviews import seed_closed_source, ReviewGateway
from tests.support.knowledge import FakeEmbedding

ORIGIN = 'http://127.0.0.1:8000'
HEADERS = {'Origin':ORIGIN, 'X-RetailQ-Console':'1'}


@pytest.fixture
def api_env(test_database, tmp_path, monkeypatch):
    import retailq.app as module
    import retailq.knowledge.runtime as runtime_module
    class Chat:
        def __init__(self, settings):
            pass
        async def aclose(self):
            pass
    @contextmanager
    def embedding(settings, budget):
        instance = FakeEmbedding()
        try:
            yield instance
        finally:
            instance.close()
    entered, release = Event(), Event()
    release.set()
    class Gateway(ReviewGateway):
        def extract(self, batch):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test must release model')
            return super().extract(batch)
    @contextmanager
    def qa(settings):
        gateway = Gateway(test_database)
        try:
            yield gateway
        finally:
            gateway.close()
    monkeypatch.setattr(module, 'OpenAIModelGateway', Chat)
    monkeypatch.setattr(runtime_module, 'SiliconFlowEmbedding', embedding)
    monkeypatch.setattr(runtime_module, 'OpenAIQAGateway', qa)
    local = Settings()
    settings = Settings(_env_file=None, mysql_port=3307, mysql_database='retailq_test',
        mysql_user=local.mysql_user, mysql_password=local.mysql_password, knowledge_enabled=True,
        knowledge_schedule_enabled=False, knowledge_data_dir=str(tmp_path),
        knowledge_tokenizer_path=str(Path('artifacts/knowledge/assets/tokenizer.json').resolve()),
        embedding_api_key='test', openai_base_url='http://model.test/v1', openai_api_key='test',
        openai_model='deterministic-test', context_window_tokens=31536)
    application = module.create_app(settings)
    with TestClient(application, base_url=ORIGIN, client=('127.0.0.1',50000)) as client:
        runtime = application.state.knowledge_runtime
        env = SimpleNamespace(client=client, app=application, runtime=runtime, database=test_database,
                              entered=entered, release=release, root=tmp_path)
        try:
            yield env
        finally:
            release.set()


def key():
    return uuid.uuid4().hex


def wait(env, operation_id):
    return env.client.portal.call(env.app.state.review_jobs.wait, operation_id)


def prepare(env):
    source = seed_closed_source(env.database)
    response = env.client.post('/api/knowledge/runs', json={
        'request_key':key(), 'conversation_ids':[str(source.conversation_id)]}, headers=HEADERS)
    assert response.status_code == 202, response.text
    operation = response.json()
    assert wait(env, operation['operation_id']).phase == 'succeeded'
    rid = operation['run_id']
    groups = env.client.get(f'/api/knowledge/runs/{rid}/groups').json()
    return rid, groups['items'][0]


def test_decision_api_persists_command_before_202(api_env):
    env = api_env
    rid, group = prepare(env)
    operation_id = key()
    body = {'request_key':operation_id, 'expected_revision':group['revision'],
            'decision':'approve', 'category':'售后手册'}
    url = f'/api/knowledge/runs/{rid}/groups/{group["group_id"]}/decision'
    response = env.client.post(url, json=body, headers=HEADERS)
    assert response.status_code == 202, response.text
    assert env.runtime.review_store.get_operation(operation_id).kind == 'decide'
    assert wait(env, operation_id).phase == 'succeeded'
    detail = env.client.get(url.removesuffix('/decision')).json()
    assert detail['target_confirmed'] is True
    assert isinstance(detail['target_chunk_id'], str)
    chunk = env.client.get('/api/knowledge/chunks/' + detail['target_chunk_id']).json()
    assert chunk['record']['draft']['category'] == '售后手册'
    assert env.client.post(url, json=body, headers=HEADERS).status_code == 200
    opposite = {**body, 'request_key':key(), 'decision':'reject', 'category':None}
    assert env.client.post(url, json=opposite, headers=HEADERS).status_code == 409


@pytest.mark.parametrize('headers', [
    {}, {'X-RetailQ-Console':'1'}, {'Origin':ORIGIN},
    {'Origin':'http://evil.example','X-RetailQ-Console':'1'},
    {'Origin':'http://127.0.0.1:8001','X-RetailQ-Console':'1'},
    {'Origin':'null','X-RetailQ-Console':'1'},
    {'Origin':ORIGIN,'X-RetailQ-Console':'0'},
])
def test_unsafe_write_has_no_side_effects(api_env, headers):
    env = api_env
    before = tuple(env.runtime.review_store.list_operations())
    response = env.client.post('/api/knowledge/runs', json={'request_key':key()}, headers=headers)
    assert response.status_code == 403
    assert env.runtime.review_store.list_operations() == before
    assert env.runtime.service.repository.all_chunks() == []


def test_rejects_untrusted_host_and_forwarded_client(api_env):
    env = api_env
    assert env.client.get('/api/knowledge/summary', headers={'Host':'evil.example'}).status_code == 403
    # A forged header never turns an actual nonloopback peer into a local client.
    # No context entry: share the already-running application's lifespan.
    remote = TestClient(env.app, base_url=ORIGIN, client=('192.0.2.7',50000))
    try:
        assert remote.get('/api/knowledge/summary', headers={'X-Forwarded-For':'127.0.0.1'}).status_code == 403
    finally:
        remote.close()


@pytest.mark.parametrize('ids', [[1], [True], ['01'], ['１２'], [str(2**63)], ['1','1'], [], [str(i) for i in range(1,102)]])
def test_source_ids_are_strict_canonical_strings(api_env, ids):
    response = api_env.client.post('/api/knowledge/runs', json={'request_key':key(), 'conversation_ids':ids}, headers=HEADERS)
    assert response.status_code == 422
    assert api_env.runtime.review_store.list_operations() == ()


def test_bigint_responses_keep_exact_decimal_digits(api_env):
    env = api_env
    cid = 9007199254740993
    with env.database.sessions.begin() as session:
        session.add(KnowledgeChunk(id=cid, category='类', questions='问', answer='完整答案。', vectorize_status='pending'))
    data = env.client.get(f'/api/knowledge/chunks/{cid}').json()
    assert data['record']['id'] == '9007199254740993'


def test_source_detail_has_complete_snapshot_and_string_message_ids(api_env):
    source = seed_closed_source(api_env.database)
    response = api_env.client.get(f'/api/knowledge/sources/{source.conversation_id}')
    assert response.status_code == 200
    data = response.json()
    assert data['conversation_id'] == str(source.conversation_id)
    assert [m['id'] for m in data['messages']] == [str(m.id) for m in source.messages]


def test_guard_precedes_json_parsing_and_input_bounds_are_enforced(api_env):
    env = api_env
    assert env.client.post('/api/knowledge/runs', content='{broken', headers={'Content-Type':'application/json'}).status_code == 403
    assert env.client.get('/api/knowledge/sources?limit=101').status_code == 422
    assert env.client.get('/api/knowledge/chunks/01').status_code == 422
    assert env.client.post('/api/knowledge/runs', json={'request_key':key(), 'target_chunk_id':'1'}, headers=HEADERS).status_code == 422
    assert env.runtime.review_store.list_operations() == ()


def test_http_client_disconnect_does_not_cancel_accepted_job(api_env):
    env = api_env
    env.release.clear()
    source = seed_closed_source(env.database)
    response = env.client.post('/api/knowledge/runs', json={'request_key':key(),
        'conversation_ids':[str(source.conversation_id)]}, headers=HEADERS)
    assert response.status_code == 202
    assert env.entered.wait(3)
    env.client.close()
    env.release.set()
    assert wait(env, response.json()['operation_id']).phase == 'succeeded'
    assert env.runtime.service.repository.all_chunks() == []


def test_disabled_knowledge_is_503_and_health_contract_unchanged():
    from retailq.app import create_app
    with TestClient(create_app(Settings(_env_file=None)), base_url=ORIGIN, client=('127.0.0.1',50000)) as client:
        assert client.get('/api/knowledge/summary').status_code == 503
        assert client.get('/health').json() == {'status':'ok','model_configured':False,'database_configured':False}


def test_duplicate_seal_with_new_key_does_not_block_new_task(api_env):
    env = api_env
    rid, group = prepare(env)
    url = f'/api/knowledge/runs/{rid}/seal'
    for _ in range(2):
        response = env.client.post(url, json={'request_key':key(),'confirm_run_id':rid}, headers=HEADERS)
        assert response.status_code == 202, response.text
        assert wait(env, response.json()['operation_id']).phase == 'sealed'
    source = seed_closed_source(env.database, '另一问法', '另一答案。')
    response = env.client.post('/api/knowledge/runs', json={'request_key':key(),
        'conversation_ids':[str(source.conversation_id)]}, headers=HEADERS)
    assert response.status_code == 202, response.text
    wait(env, response.json()['operation_id'])


def test_processed_source_is_visible_but_not_selectable(api_env):
    env = api_env
    rid, group = prepare(env)
    response = env.client.post(f'/api/knowledge/runs/{rid}/groups/{group["group_id"]}/decision',
        json={'request_key':key(),'expected_revision':group['revision'],'decision':'approve','category':'售后手册'},
        headers=HEADERS)
    wait(env, response.json()['operation_id'])
    cid = env.runtime.review_store.get_run(rid).sources[0].conversation_id
    data = env.client.get('/api/knowledge/sources', params={'query':str(cid)}).json()
    source = next(s for s in data['items'] if s['id'] == str(cid))
    assert source['eligibility'] is False and '处理' in source['reason']
    assert env.client.get('/api/knowledge/sources', params={'query':str(cid),'eligibility':'eligible'}).json()['total'] == 0


def test_unreviewed_qa_is_not_retrievable_then_reviewed_category_and_source_survive_reopen(api_env):
    from retailq.app import create_app
    env = api_env
    rid, group = prepare(env)
    assert env.runtime.service.repository.all_chunks() == []
    assert env.runtime.service.search_faq('如何保养') == []
    response = env.client.post(f'/api/knowledge/runs/{rid}/groups/{group["group_id"]}/decision',
        json={'request_key':key(),'expected_revision':group['revision'],'decision':'approve','category':'人工确认的已有类'},
        headers=HEADERS)
    assert wait(env,response.json()['operation_id']).phase == 'succeeded'
    detail_path = f'/api/knowledge/runs/{rid}/groups/{group["group_id"]}'
    before = env.client.get(detail_path).json()
    settings = env.runtime.settings
    env.client.__exit__(None,None,None)
    with TestClient(create_app(settings),base_url=ORIGIN,client=('127.0.0.1',50000)) as reopened:
        after = reopened.get(detail_path).json()
        assert after['target_confirmed'] and after['target_chunk_id'] == before['target_chunk_id']
        assert after['members'] == before['members'] and after['dispositions'] == before['dispositions']
        chunk = reopened.get('/api/knowledge/chunks/'+after['target_chunk_id']).json()['record']
        assert chunk['draft']['category'] == '人工确认的已有类' and chunk['vectorize_status'] == 'done'
        hits = reopened.app.state.knowledge_runtime.service.search_faq('如何保养')
        assert len(hits) == 1 and str(hits[0]['id']) == after['target_chunk_id']


def test_seal_and_restart_release_slot_without_reviving_approval(api_env):
    from retailq.app import create_app
    from retailq.knowledge.review_contracts import DecisionCommand
    env = api_env
    rid, group = prepare(env)
    command = DecisionCommand(request_key=key(),run_id=rid,group_id=group['group_id'],
        expected_revision=group['revision'],decision='approve',category='不得复活')
    admission = env.runtime.admit_review(command)
    admission.lease.close()  # Accepted command, deliberately no business worker yet.
    response = env.client.post(f'/api/knowledge/runs/{rid}/seal',
        json={'request_key':key(),'confirm_run_id':rid},headers=HEADERS)
    assert wait(env,response.json()['operation_id']).phase == 'sealed'
    settings = env.runtime.settings
    env.client.__exit__(None,None,None)
    with TestClient(create_app(settings),base_url=ORIGIN,client=('127.0.0.1',50000)) as reopened:
        assert reopened.get('/api/knowledge/summary').json()['current_run_id'] is None
        assert reopened.get(f'/api/knowledge/operations/{command.request_key}').json()['phase'] == 'sealed'
        assert reopened.app.state.knowledge_runtime.service.repository.all_chunks() == []
        source = seed_closed_source(env.database,'重新开启的来源','另一份完整答案。')
        response = reopened.post('/api/knowledge/runs',json={'request_key':key(),
            'conversation_ids':[str(source.conversation_id)]},headers=HEADERS)
        assert response.status_code == 202
        assert reopened.portal.call(reopened.app.state.review_jobs.wait,response.json()['operation_id']).phase == 'succeeded'


def test_queued_prepare_projects_before_worker_creates_run_file(api_env):
    from retailq.knowledge.review_contracts import PrepareCommand
    env = api_env
    source = seed_closed_source(env.database)
    command = PrepareCommand(request_key=key(),conversation_ids=(source.conversation_id,))
    admission = env.runtime.admit_review(command)
    try:
        before = {p.relative_to(env.root):p.read_bytes() for p in env.root.rglob('*.json')}
        summary = env.client.get('/api/knowledge/summary')
        assert summary.status_code == 200, summary.text
        assert summary.json()['current_run_id'] == command.request_key
        detail = env.client.get('/api/knowledge/runs/'+command.request_key)
        assert detail.status_code == 200, detail.text
        assert detail.json()['sources'][0]['conversation_id'] == str(source.conversation_id)
        groups = env.client.get('/api/knowledge/runs/'+command.request_key+'/groups')
        assert groups.status_code == 200 and groups.json()['items'] == []
        assert before == {p.relative_to(env.root):p.read_bytes() for p in env.root.rglob('*.json')}
    finally:
        admission.lease.close()
