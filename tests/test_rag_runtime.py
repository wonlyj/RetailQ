import json
import time
import httpx
import pytest
from retailq.config import Settings
from retailq.errors import ServiceError
from retailq.knowledge.query_gateway import QueryGateway


def test_query_gateway_preserves_current_question_and_rejects_incomplete_response():
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':'MH-LP100 运费是多少？'}}]})
    settings = Settings(_env_file=None, openai_base_url='https://example.test/v1', openai_api_key='secret', openai_model='model')
    gateway = QueryGateway(settings, transport=httpx.MockTransport(handler))
    assert gateway.normalize('MH-LP100 邮费咋算', time.monotonic()+5) == 'MH-LP100 运费是多少？'
    assert seen[0]['messages'][-1]['content'] == 'MH-LP100 邮费咋算'
    assert len(seen[0]['messages']) == 2
    gateway.close()
    gateway = QueryGateway(settings, transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':'截断'}}]})))
    with pytest.raises(ServiceError): gateway.normalize('问题', time.monotonic()+5)
    gateway.close()


@pytest.mark.mysql
def test_two_runtimes_open_without_exclusive_directory_owner(test_database, tmp_path):
    from pathlib import Path
    from retailq.coordination import Coordinator, RuntimeIdentity
    from retailq.knowledge.runtime import KnowledgeRuntime
    root = tmp_path/'knowledge'
    coordinator = Coordinator(tmp_path/'locks', RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',root))
    settings = Settings(_env_file=None, knowledge_enabled=True, knowledge_data_dir=str(root),
                        knowledge_tokenizer_path='artifacts/knowledge/assets/tokenizer.json',
                        mysql_host='127.0.0.1',mysql_port=3307,mysql_database='retailq_test',mysql_password='test',
                        embedding_api_key='fixture')
    if not Path(settings.knowledge_tokenizer_path).exists(): pytest.skip('tokenizer asset unavailable')
    first = KnowledgeRuntime.open(settings, test_database, coordinator=coordinator)
    try:
        second = KnowledgeRuntime.open(settings, test_database, coordinator=coordinator)
        try:
            assert first.service.status()['active'] == 0
            assert second.service.status()['history'] == 0
        finally: second.close()
    finally: first.close()


@pytest.mark.mysql
def test_runtime_build_publish_and_bm25_query_with_standalone(test_database, tmp_path, monkeypatch):
    from pathlib import Path
    from threading import Event
    from uuid import uuid4
    from pymilvus import MilvusClient
    from retailq.coordination import Coordinator, RuntimeIdentity
    from retailq.knowledge.runtime import KnowledgeRuntime
    from tests.support.milvus import require_isolated_milvus
    root = tmp_path/'knowledge'
    uri = 'http://127.0.0.1:19531'
    coordinator = Coordinator(tmp_path/'locks', RuntimeIdentity('127.0.0.1',3307,'retailq_test',uri,root))
    settings = Settings(_env_file=None,knowledge_enabled=True,knowledge_data_dir=str(root),milvus_uri=uri,milvus_collection_prefix='test',
                        knowledge_tokenizer_path='artifacts/knowledge/assets/tokenizer.json',mysql_host='127.0.0.1',mysql_port=3307,
                        mysql_database='retailq_test',mysql_password='test',embedding_api_key='fixture',
                        openai_base_url='https://example.test/v1',openai_api_key='fixture',openai_model='fixture')
    if not Path(settings.knowledge_tokenizer_path).exists(): pytest.skip('tokenizer asset unavailable')
    source=tmp_path/'source';source.mkdir()
    (source/'models.md').write_text('# 规格\n## 智能猫砂盆 MH-LP100\n演示型号支持自动清洁。\n',encoding='utf-8')
    (source/'manifest.json').write_text(json.dumps({'files':[{'path':'models.md','content_type':'manual'}]}),encoding='utf-8')
    build_id=uuid4().hex;collection='test_'+build_id
    require_isolated_milvus(uri,collection)
    runtime=KnowledgeRuntime.open(settings,test_database,coordinator=coordinator)
    try:
        monkeypatch.setattr(runtime.embedding,'embed',lambda texts,**kwargs:[[1.]+[0.]*1023 for _ in texts])
        monkeypatch.setattr(runtime.query_gateway,'normalize',lambda question,deadline:question)
        prepared=runtime.maintain('prepare-corpus',{'manifest':str(source/'manifest.json'),'build_id':build_id},Event())
        assert prepared['phase']=='sql_committed'
        report=runtime.maintain('build-index',{'build_id':build_id},Event())
        assert report['phase']=='verified' and report['failed']==0
        assert runtime.maintain('publish-index',{'build_id':build_id},Event())['generation']==1
        result=runtime.retrieve('MH-LP100',strategy='bm25')
        assert result.candidates and 'MH-LP100' in result.candidates[0].draft.questions
    finally:
        runtime.close()
        client=MilvusClient(uri)
        require_isolated_milvus(uri,collection)
        if client.has_collection(collection):client.drop_collection(collection)
        client.close()
