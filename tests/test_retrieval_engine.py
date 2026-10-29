from types import SimpleNamespace
import time

from retailq.knowledge.contracts import ChunkDraft, ChunkRecord
from retailq.knowledge.corpus import CorpusSnapshot
from retailq.knowledge.retrieval import RetrievalEngine
from retailq.knowledge.retrieval_contracts import QueryUnderstanding, RawHit, RetrievalFilters
from retailq.knowledge.text import content_fingerprint


def test_four_strategies_share_understanding_and_drop_stale_and_pending():
    draft = ChunkDraft('分类', '问题', '答案', None, 'faq')
    records = {
        1: ChunkRecord(1, draft, None, None, '1', 'done'),
        2: ChunkRecord(2, draft, None, None, '2', 'done'),
        3: ChunkRecord(3, draft, None, None, None, 'pending'),
    }
    fp = content_fingerprint(draft)
    snapshot = CorpusSnapshot(1, 'collection', 'x', (), (1, 2, 3), (), ((1, fp), (2, fp), (3, fp)), 'hash', (1, 2, 3), 'build')
    class Repo:
        def get_chunks(self, ids):
            return {i: records[i] for i in ids}
    class Embedding:
        calls = 0
        def embed(self, queries, deadline, retries):
            assert retries == 0
            self.calls += 1
            return [[1.0] + [0.0] * 1023]
    class Store:
        collection = 'collection'
        def dense_search(self, vector, limit, filters, *, timeout_seconds):
            return hits
        def bm25_search(self, text, limit, filters, *, timeout_seconds):
            return hits
        def hybrid_search(self, vector, text, filters, *, timeout_seconds):
            return hits
    class Rerank:
        def rerank(self, query, candidates, *, deadline):
            assert query == '原问题'
            return candidates
    hits = (RawHit(1, 0.001, fp), RawHit(2, 0.9, '0'*64), RawHit(3, 0.8, fp))
    embedding = Embedding()
    engine = RetrievalEngine(Repo(), embedding, Store(), Rerank())
    understood = QueryUnderstanding('原问题', '标准问题', '标准问题 扩展')
    for strategy in ('dense', 'bm25', 'hybrid', 'hybrid_rerank'):
        result = engine.retrieve(understood, snapshot, strategy, RetrievalFilters(), deadline=time.monotonic()+5)
        assert result.understanding is understood and result.snapshot is snapshot
        assert [item.id for item in result.candidates] == [1]
        assert [hit.id for hit in result.candidate_hits] == [1]
    assert embedding.calls == 3


def test_mismatched_collection_and_expired_deadline_fail_closed():
    import pytest
    from retailq.errors import ServiceError
    engine = RetrievalEngine(None, None, SimpleNamespace(collection='wrong'), None)
    snapshot = SimpleNamespace(collection='active')
    understood = QueryUnderstanding('问题', '问题', '问题')
    with pytest.raises(ServiceError):
        engine.retrieve(understood, snapshot, 'bm25', RetrievalFilters(), deadline=time.monotonic()+5)
    engine.store.collection = 'active'
    with pytest.raises(ServiceError):
        engine.retrieve(understood, snapshot, 'bm25', RetrievalFilters(), deadline=time.monotonic()-1)
