import uuid

import pytest
from pymilvus import MilvusClient

from tests.support.milvus import require_isolated_milvus
from retailq.knowledge.milvus_store import MilvusKnowledgeStore
from retailq.knowledge.retrieval_contracts import IndexedChunk, RetrievalFilters


@pytest.fixture
def standalone():
    uri = 'http://127.0.0.1:19531'
    collection = 'test_' + uuid.uuid4().hex
    require_isolated_milvus(uri, collection)
    store = MilvusKnowledgeStore(uri, {'model': 'test'}, collection=collection)
    try:
        store.ensure_collection()
        yield store
    finally:
        store.close()
        client = MilvusClient(uri)
        require_isolated_milvus(uri, collection)
        if client.has_collection(collection):
            client.drop_collection(collection)
        client.close()


def test_bm25_hits_literal_model(standalone):
    vector = [1.0] + [0.0] * 1023
    items = [IndexedChunk(101, vector, 'a'*64, '智能猫砂盆 Pro 型号 MH-LP100 适用猫咪', '智能猫砂盆', 'manual', 'source', 'version'),
             IndexedChunk(102, vector, 'b'*64, '自动饮水机 型号 MH-W20 水箱容量', '饮水机', 'manual', 'source', 'version')]
    assert set(standalone.upsert(items)) == {101, 102}
    hits = standalone.bm25_search('MH-LP100', 50, RetrievalFilters(category='智能猫砂盆'), timeout_seconds=10)
    assert 101 in [hit.id for hit in hits]
    assert 102 not in [hit.id for hit in hits]
    assert standalone.hybrid_search(vector, 'MH-LP100', RetrievalFilters(category='智能猫砂盆'), timeout_seconds=10)[0].id == 101


def test_two_clients_and_schema_identity(standalone):
    second = MilvusKnowledgeStore(standalone.uri, {'model': 'test'}, collection=standalone.collection)
    try:
        second.ensure_collection()
    finally:
        second.close()
    other = MilvusKnowledgeStore(standalone.uri, {'model': 'other'}, collection=standalone.collection)
    try:
        with pytest.raises(Exception):
            other.ensure_collection()
    finally:
        other.close()


def test_filters_apply_to_all_paths_and_escape_literals(standalone):
    vector = [1.0] + [0.0] * 1023
    category = '类别"反斜线\\百分%下划线_'
    standalone.upsert([IndexedChunk(201, vector, 'c'*64, '型号 MH-LP100', category, 'manual', 'source', 'version'),
                       IndexedChunk(202, vector, 'd'*64, '型号 MH-LP100', '其他', 'faq', 'source', 'version')])
    filters = RetrievalFilters(category=category, content_type='manual')
    assert [x.id for x in standalone.dense_search(vector, 50, filters, timeout_seconds=10)] == [201]
    assert [x.id for x in standalone.bm25_search('MH-LP100', 50, filters, timeout_seconds=10)] == [201]
    assert [x.id for x in standalone.hybrid_search(vector, 'MH-LP100', filters, timeout_seconds=10)] == [201]
    assert not standalone.hybrid_search(vector, 'MH-LP100', RetrievalFilters(category='不存在'), timeout_seconds=10)


def test_category_prefix_treats_wildcards_as_literals(standalone):
    vector = [1.0] + [0.0] * 1023
    prefix = '目录%_\\'
    standalone.upsert([IndexedChunk(301, vector, 'a'*64, '型号 MH-LP100', prefix + ' / 品类', 'manual', 's', 'v'),
                       IndexedChunk(302, vector, 'b'*64, '型号 MH-LP100', '目录XY / 品类', 'manual', 's', 'v')])
    filters = RetrievalFilters(category_prefix=prefix)
    assert [h.id for h in standalone.dense_search(vector, 50, filters, timeout_seconds=10)] == [301]
    assert [h.id for h in standalone.bm25_search('MH-LP100', 50, filters, timeout_seconds=10)] == [301]
    assert [h.id for h in standalone.hybrid_search(vector, 'MH-LP100', filters, timeout_seconds=10)] == [301]


def test_schema_tampering_is_rejected(standalone, monkeypatch):
    original = standalone.client.describe_collection
    def altered(name):
        schema = original(name)
        schema['fields'][1]['params']['dim'] = 512
        return schema
    monkeypatch.setattr(standalone.client, 'describe_collection', altered)
    with pytest.raises(Exception):
        standalone.ensure_collection()


@pytest.mark.mysql
def test_literal_model_from_imported_sql_chunk(standalone, test_database, tmp_path):
    from pathlib import Path
    from retailq.coordination import Coordinator, RuntimeIdentity
    from retailq.knowledge.corpus import CorpusStore
    from retailq.knowledge.corpus_import import CorpusImporter
    from retailq.knowledge.repository import KnowledgeRepository
    from retailq.knowledge.text import content_fingerprint, embedding_text
    from tests.support.knowledge import TestBudget

    root = tmp_path / 'knowledge'
    corpus = CorpusStore(root, Coordinator(tmp_path / 'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', standalone.uri, root)))
    repository = KnowledgeRepository(test_database)
    plan = CorpusImporter(repository, corpus, TestBudget()).prepare(Path('examples/knowledge/manifest.json'), operation_id='f' * 32)
    records = repository.get_chunks(list(plan.active_ids))
    matching = [r for r in records.values() if 'MH-LP100' in r.draft.questions]
    assert len(matching) == 1
    row = matching[0]
    vector = [1.0] + [0.0] * 1023
    standalone.upsert([IndexedChunk(row.id, vector, content_fingerprint(row.draft), embedding_text(row.draft), row.draft.category, row.draft.content_type, 'product-specs.md', next(s.digest for s in plan.sources if s.key == 'product-specs.md'))])
    hits = standalone.bm25_search('MH-LP100', 50, RetrievalFilters(), timeout_seconds=10)
    assert row.id in [hit.id for hit in hits]
    assert 'MH-LP100' in records[row.id].draft.questions
