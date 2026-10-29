"""Chapter-three contract fixture backed only by isolated Standalone."""
from hashlib import sha256
from pathlib import Path

from pymilvus import MilvusClient
from retailq.knowledge.contracts import SearchHit
from retailq.knowledge.milvus_store import MilvusKnowledgeStore as NativeStore
from retailq.knowledge.retrieval_contracts import IndexedChunk, RetrievalFilters
from retailq.knowledge.text import embedding_text
from tests.support.milvus import require_isolated_milvus

URI = 'http://127.0.0.1:19531'
CREATED = set()


def collection_for(path):
    name = 'test_' + sha256(str(Path(path).resolve()).encode('utf-8')).hexdigest()[:32]
    require_isolated_milvus(URI, name)
    return name


class ClientProxy:
    def __init__(self, store): self.store = store
    def delete(self, collection, *, ids):
        assert collection == 'knowledge'
        return self.store.client.delete(self.store.collection, ids=ids)
    def get_collection_stats(self, collection):
        assert collection == 'knowledge'
        return {'row_count': self.store.count()}


class MilvusKnowledgeStore:
    def __init__(self, path, identity, repository=None):
        self.path, self.repository = Path(path), repository
        self.collection = collection_for(path)
        self.native = NativeStore(URI, identity, collection=self.collection)
        if not self.native.exists(): CREATED.add(self.collection)
        try:
            self.native.ensure_collection()
        except BaseException:
            self.native.close()
            raise
        self.client = ClientProxy(self.native)

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    def upsert(self, items):
        rows = self.repository.get_chunks([item.id for item in items]) if self.repository else {}
        converted = []
        for item in items:
            row = rows.get(item.id)
            converted.append(IndexedChunk(item.id,item.vector,item.fingerprint,embedding_text(row.draft) if row else 'fixture',
                row.draft.category if row else 'fixture', row.draft.content_type or 'faq' if row else 'faq', 'fixture','v1'))
        return self.native.upsert(converted)

    def search(self, vector, limit, *, timeout_seconds=None):
        return [SearchHit(hit.id,hit.score,hit.fingerprint) for hit in self.native.dense_search(
            vector,limit,RetrievalFilters(),timeout_seconds=timeout_seconds or 10)]

    def fingerprints(self, ids): return self.native.fingerprints(ids)

    def close(self):
        if self.client is not None:
            self.native.close()
            self.client = None
