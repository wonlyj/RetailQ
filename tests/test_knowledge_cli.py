import json
from pathlib import Path

import pytest

from retailq.config import Settings


def test_cli_missing_configuration_returns_two(capsys):
    from scripts.knowledge import main
    assert main(['vectorize'],settings=Settings(_env_file=None))==2
    assert 'error' in json.loads(capsys.readouterr().out)


def test_cli_scope_guard_rejects_wrong_database(capsys):
    from scripts.knowledge import main
    assert main(['init-db','--scope','test'],settings=Settings(_env_file=None,mysql_database='retailq',mysql_port=3306,mysql_password='test'))==2


def test_cli_cleanup_failure_is_redacted(capsys):
    from scripts.knowledge import main
    class Runtime:
        def maintain(self,*args):return {'failed':0,'pending_after':0}
        def close(self):raise RuntimeError('private-close-secret')
    settings=Settings(_env_file=None,mysql_password='test',embedding_api_key='test')
    assert main(['vectorize'],settings=settings,runtime_factory=lambda *args:Runtime())==1
    output=capsys.readouterr().out
    assert 'private-close-secret' not in output and 'knowledge_close_failed' in output


def test_status_distinguishes_history_without_active_corpus(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from scripts import knowledge
    from retailq.knowledge import repository

    monkeypatch.setattr(knowledge, 'Database', lambda settings: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(repository, 'KnowledgeRepository', lambda database: SimpleNamespace(
        all_chunks=lambda: [SimpleNamespace(vectorize_status='done'), SimpleNamespace(vectorize_status='pending')]))
    settings = Settings(_env_file=None, mysql_password='test', knowledge_data_dir=str(tmp_path))
    assert knowledge.main(['status'], settings=settings) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result['active'], result['history'], result['active_pending'], result['pending']) == (0, 2, 0, 1)


@pytest.mark.mysql
@pytest.mark.standalone
def test_cli_import_repeat_vectorize_status_and_reopen_query(test_database,tmp_path,capsys):
    from scripts.knowledge import main
    from retailq.knowledge.runtime import KnowledgeRuntime
    from retailq.knowledge.ownership import DirectoryOwner
    from retailq.knowledge.repository import KnowledgeRepository
    from tests.support.standalone_legacy import MilvusKnowledgeStore
    from retailq.knowledge.service import KnowledgeService
    from tests.support.knowledge import FakeEmbedding,TestBudget
    from tests.test_knowledge_vectors import identity
    def factory(settings,database):
        owner=DirectoryOwner(Path(settings.knowledge_data_dir)).__enter__()
        repository=KnowledgeRepository(database)
        vectors=MilvusKnowledgeStore(owner.root/'knowledge.db',identity(),repository=repository)
        embed=FakeEmbedding()
        service=KnowledgeService(repository,embed,vectors,TestBudget(),min_score=0.2)
        return KnowledgeRuntime(service,owner,settings=settings,embedding=embed,vectors=vectors)
    settings=Settings(mysql_database='retailq_test',mysql_port=3307,knowledge_data_dir=str(tmp_path),embedding_api_key='test')
    for command in [['import-faq'],['import-faq'],['vectorize'],['status'],['query','--text','配送查询']]:
        assert main(command,settings=settings,runtime_factory=factory)==0
        result=json.loads(capsys.readouterr().out)
        if command[0]=='query':assert result['found'] and result['items'][0]['category']=='配送'
    from retailq.knowledge.repository import KnowledgeRepository
    assert len(KnowledgeRepository(test_database).all_chunks())==3
