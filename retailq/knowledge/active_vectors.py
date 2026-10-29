"""Resolve the current active collection only at an operation boundary."""
from contextlib import contextmanager

from retailq.errors import ServiceError
from retailq.knowledge.milvus_store import MilvusKnowledgeStore


class ActiveVectors:
    def __init__(self, uri, identity, corpus):
        self.uri, self.identity, self.corpus = uri, identity, corpus

    @property
    def collection(self): return self.corpus.active().collection

    @contextmanager
    def open(self, collection=None):
        target = collection or self.collection
        store = MilvusKnowledgeStore(self.uri, self.identity, collection=target)
        try:
            if not store.exists():
                raise ServiceError('knowledge_corpus_invalid',409,'活动知识集合不存在。')
            store.ensure_collection()
            yield store
        finally:
            store.close()

    def upsert(self, items):
        with self.open() as store: return store.upsert(items)

    def fingerprints(self, ids):
        with self.open() as store: return store.fingerprints(ids)

    def close(self): pass
