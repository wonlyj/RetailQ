import math

import pytest

from retailq.knowledge.milvus_store import MilvusKnowledgeStore
from retailq.knowledge.retrieval_contracts import IndexedChunk, RetrievalFilters


@pytest.mark.parametrize('vector', [[0.0] * 1024, [1.0], [math.nan] * 1024, [math.inf] * 1024])
def test_invalid_vectors_are_rejected(vector):
    with pytest.raises(ValueError):
        MilvusKnowledgeStore._vector(vector)


@pytest.mark.parametrize('category', ['', '猫' * 342])
def test_filter_byte_limit(category):
    with pytest.raises(ValueError):
        MilvusKnowledgeStore._filter(RetrievalFilters(category=category))


def test_upsert_validates_before_contacting_server():
    store = object.__new__(MilvusKnowledgeStore)
    vector = [1.0] + [0.0] * 1023
    for item in [IndexedChunk(2**63, vector, 'a'*64, '内容', '分类', 'manual', 'source', 'version'),
                 IndexedChunk(1, vector, 'a'*64, '猫'*21846, '分类', 'manual', 'source', 'version'),
                 IndexedChunk(1, vector, 'z'*64, '内容', '分类', 'manual', 'source', 'version')]:
        with pytest.raises(ValueError):
            store.upsert([item])


def test_upsert_rejects_partial_receipt_and_duplicate_ids():
    from retailq.errors import ServiceError

    class Client:
        def upsert(self, *args):
            return {'ids': [1], 'upsert_count': 1}
    store = object.__new__(MilvusKnowledgeStore)
    store.collection, store.client = 'test', Client()
    vector = [1.0] + [0.0] * 1023
    first = IndexedChunk(1, vector, 'a'*64, '内容', '分类', 'manual', 'source', 'version')
    second = IndexedChunk(2, vector, 'b'*64, '内容', '分类', 'manual', 'source', 'version')
    with pytest.raises(ServiceError):
        store.upsert([first, second])
    with pytest.raises(ValueError):
        store.upsert([first, first])
