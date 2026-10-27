from types import SimpleNamespace
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from retailq.config import Settings
from retailq.errors import ServiceError
from retailq.repositories import ChatRepository
from retailq.tools.runtime import ToolContext,build_registry
from retailq.tools.schemas import FAQInput
from tests.support.knowledge import service


def test_importing_disabled_app_does_not_populate_environment():
    environment={k:v for k,v in os.environ.items() if not k.startswith(('OPENAI_','MYSQL_','EMBEDDING_','KNOWLEDGE_'))}
    code='import os; import retailq.app; assert not any(k in os.environ for k in ["OPENAI_API_KEY","MYSQL_PASSWORD","EMBEDDING_API_KEY"])'
    result=subprocess.run([sys.executable,'-c',code],env=environment,capture_output=True,text=True,timeout=15)
    assert result.returncode==0


@pytest.mark.mysql
@pytest.mark.lite
@pytest.mark.anyio
async def test_faq_schema_and_result_shape_are_unchanged(service):
    schema=FAQInput.model_json_schema()
    assert schema['required']==['keyword'] and schema['additionalProperties'] is False
    assert schema['properties']['keyword']['minLength']==1 and schema['properties']['keyword']['maxLength']==64
    assert schema['properties']['keyword']['type']=='string'
    repo=ChatRepository(service.repository.database,faq_search=service.search_faq)
    result=await build_registry(repo,ToolContext(1,'user'),Settings(_env_file=None)).execute({'name':'query_faq','args':{'keyword':'配送查询'}})
    assert result.payload['ok'] and set(result.payload['data'])=={'found','items'}
    assert set(result.payload['data']['items'][0])=={'id','question','answer','category'}


@pytest.mark.mysql
def test_faq_repository_delegates_without_sql_like(test_database):
    statements=[]
    def observed(connection,cursor,statement,parameters,context,executemany):statements.append(statement)
    event.listen(test_database.engine,'before_cursor_execute',observed)
    try:
        repository=ChatRepository(test_database,faq_search=lambda keyword:[{'id':7,'question':keyword,'answer':'事实。','category':'演示'}])
        assert repository.search_faq('邮费')[0]['id']==7
        assert statements==[]
    finally:event.remove(test_database.engine,'before_cursor_execute',observed)


@pytest.mark.anyio
async def test_missing_knowledge_fails_only_faq_tool():
    repo=ChatRepository(None)
    with pytest.raises(ServiceError) as error:repo.search_faq('邮费')
    assert error.value.code=='knowledge_not_configured'
    registry=build_registry(repo,ToolContext(1,'user'),Settings(_env_file=None))
    assert not (await registry.execute({'name':'query_faq','args':{'keyword':'邮费'}})).payload['ok']
    assert (await registry.execute({'name':'query_order','args':{'order_id':'001'}})).payload['ok']


@pytest.mark.anyio
async def test_embedding_fault_is_not_a_successful_miss():
    def unavailable(keyword):raise ServiceError('embedding_unavailable',503,'暂不可用')
    repo=ChatRepository(None,faq_search=unavailable)
    result=await build_registry(repo,ToolContext(1,'u'),Settings(_env_file=None)).execute({'name':'query_faq','args':{'keyword':'邮费'}})
    assert result.payload['ok'] is False and 'data' not in result.payload


@pytest.mark.anyio
async def test_faq_retry_is_not_multiplied():
    calls=[]
    def unavailable(keyword):calls.append(keyword);raise ConnectionError('failure')
    repo=ChatRepository(None,faq_search=unavailable)
    await build_registry(repo,ToolContext(1,'u'),Settings(_env_file=None)).execute({'name':'query_faq','args':{'keyword':'邮费'}})
    assert calls==['邮费']


def test_lifespan_owns_one_store_and_shutdown_order(monkeypatch):
    import retailq.app as module
    events=[]
    class DB:
        def __init__(self,settings):pass
        def close(self):events.append('db-close')
    class Gateway:
        def __init__(self,settings):pass
        async def aclose(self):events.append('gateway-close')
    class Knowledge:
        can_prepare_review=True
        def search_faq(self,query):return []
        def close(self):events.append('knowledge-close')
    def opened(settings,database):events.append('knowledge-open');return Knowledge()
    class Scheduler:
        def __init__(self,runtime):pass
        def start(self):events.append('schedule-start')
        async def stop(self):events.append('schedule-stop')
    class Jobs:
        def __init__(self,runtime):events.append('jobs-open')
        async def aclose(self):events.append('jobs-close')
    monkeypatch.setattr(module,'Database',DB)
    monkeypatch.setattr(module,'OpenAIModelGateway',Gateway)
    monkeypatch.setattr(module,'KnowledgeRuntime',SimpleNamespace(open=opened))
    monkeypatch.setattr(module,'DailyKnowledgeScheduler',Scheduler)
    monkeypatch.setattr(module,'ReviewJobs',Jobs)
    settings=Settings(_env_file=None,knowledge_enabled=True,embedding_api_key='test',mysql_password='test',openai_api_key='test',openai_model='test',openai_base_url='http://model.test/v1')
    application=module.create_app(settings)
    with TestClient(application) as client:
        assert client.get('/health').json()['model_configured'] is True
        assert application.state.knowledge_runtime is not None
    assert events==['knowledge-open','jobs-open','schedule-start','jobs-close','schedule-stop','knowledge-close','db-close','gateway-close']
