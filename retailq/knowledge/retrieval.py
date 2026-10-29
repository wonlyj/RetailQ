"""One retrieval path shared by online usage and strategy evaluation."""
import time

from retailq.errors import ServiceError
from retailq.knowledge.corpus import is_active
from retailq.knowledge.retrieval_contracts import EvidenceChunk, RetrievalResult


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ServiceError('knowledge_query_timeout', 504, '知识查询等待超时，请稍后重试。')
    return remaining


class RetrievalEngine:
    def __init__(self, repository, embedding, store, reranker):
        self.repository, self.embedding, self.store, self.reranker = repository, embedding, store, reranker

    def retrieve(self, understood, snapshot, strategy, filters, *, deadline):
        if strategy not in ('dense', 'bm25', 'hybrid', 'hybrid_rerank'):
            raise ValueError('invalid strategy')
        if self.store.collection != snapshot.collection:
            raise ServiceError('knowledge_identity_mismatch', 409, '检索集合与活动语料不一致，请核查。')
        _remaining(deadline)
        vector = None
        if strategy != 'bm25':
            _remaining(deadline)
            vector = self.embedding.embed([understood.normalized_query], deadline=deadline, retries=0)[0]
        if strategy == 'dense':
            hits = self.store.dense_search(vector, 50, filters, timeout_seconds=_remaining(deadline))
        elif strategy == 'bm25':
            hits = self.store.bm25_search(understood.bm25_query, 50, filters, timeout_seconds=_remaining(deadline))
        else:
            hits = self.store.hybrid_search(vector, understood.bm25_query, filters, timeout_seconds=_remaining(deadline))
        _remaining(deadline)
        records = self.repository.get_chunks([hit.id for hit in hits])
        candidates = []
        seen = set()
        for rank, hit in enumerate(hits, 1):
            row = records.get(hit.id)
            if hit.id in seen or row is None or row.vectorize_status != 'done' or row.vector_id != str(row.id):
                continue
            seen.add(hit.id)
            if filters and ((filters.category is not None and row.draft.category != filters.category) or
                            (filters.content_type is not None and row.draft.content_type != filters.content_type) or
                            (filters.category_prefix is not None and row.draft.category != filters.category_prefix and
                             not row.draft.category.startswith(filters.category_prefix + ' / '))):
                continue
            if not is_active(row, snapshot) or dict(snapshot.fingerprints).get(row.id) != hit.fingerprint:
                continue
            candidates.append(EvidenceChunk(row.id, row.draft, rank, hit.score))
        if strategy == 'hybrid_rerank':
            candidates = self.reranker.rerank(understood.raw_question, tuple(candidates), deadline=deadline)
        return RetrievalResult(understood, snapshot, strategy, tuple(candidates[:10]), tuple(hits))
