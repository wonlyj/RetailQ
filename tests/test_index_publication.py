from dataclasses import replace
from pathlib import Path
from threading import Event
import json

import pytest

from retailq.coordination import Coordinator, RuntimeIdentity
from retailq.errors import ServiceError
from retailq.knowledge.corpus import CorpusPlan, CorpusStore
from retailq.knowledge.corpus_import import CorpusImporter
from retailq.knowledge.indexing import IndexBuilder
from retailq.knowledge.repository import KnowledgeRepository
from tests.support.knowledge import TestBudget
from tests.support.reviews import review_harness


@pytest.mark.mysql
def test_build_does_not_publish_partial_index(test_database, tmp_path, monkeypatch):
    root = tmp_path / 'knowledge'
    coordinator = Coordinator(tmp_path / 'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root))
    corpus = CorpusStore(root, coordinator)
    old = CorpusPlan('a'*32, 0, 'knowledge_a', 'b'*64, (), (), (), (), 'prepared')
    corpus.stage(old)
    for phase, next_phase in [('prepared', 'sql_committed'), ('sql_committed', 'indexing'), ('indexing', 'verified')]:
        old = replace(old, phase=next_phase)
        corpus.update(old, expected_phase=phase)
    old_snapshot = corpus.publish(old.build_id, expected_generation=0)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'policy.md').write_text('# 政策\n## 运费\n未满收10元。\n', encoding='utf-8')
    manifest = source / 'manifest.json'
    manifest.write_text(json.dumps({'files': [{'path': 'policy.md', 'content_type': 'policy'}]}), encoding='utf-8')
    repository = KnowledgeRepository(test_database)
    importer = CorpusImporter(repository, corpus, TestBudget())
    plan = importer.prepare(manifest, operation_id='b'*32)
    class Embedding:
        fail = True
        def embed(self, texts):
            if self.fail:
                raise RuntimeError('simulated failure')
            return [[1.] + [0.] * 1023 for _ in texts]
    class Store:
        collection = plan.collection
        identity = 'c'*64
        data = {}
        present = True
        def exists(self): return self.present
        def ensure_collection(self): pass
        def fingerprints(self, ids): return {i: self.data[i] for i in ids if i in self.data}
        def upsert(self, items):
            self.data.update((item.id, item.fingerprint) for item in items)
            return [item.id for item in items]
        def count(self): return len(self.data)
        def close(self): pass
    embed, store = Embedding(), Store()
    builder = IndexBuilder(repository, corpus, importer, coordinator, embed, lambda _: store)
    report = builder.build(plan, stop=Event())
    assert report.failed and corpus.active().generation == old_snapshot.generation
    embed.fail = False
    mark_done = repository.mark_done
    monkeypatch.setattr(repository, 'mark_done', lambda *args: False)
    report = builder.retry(plan.build_id, stop=Event())
    assert report.failed and store.data and corpus.active().generation == old_snapshot.generation
    monkeypatch.setattr(repository, 'mark_done', mark_done)
    report = builder.retry(plan.build_id, stop=Event())
    assert report.phase == 'verified' and report.indexed == report.expected
    assert corpus.active().generation == old_snapshot.generation
    store.present = False
    with pytest.raises(ServiceError):
        builder.publish(plan.build_id)
    assert corpus.active().generation == old_snapshot.generation
    store.present = True
    store.data[999] = 'd'*64
    with pytest.raises(ServiceError):
        builder.publish(plan.build_id)
    assert corpus.active().generation == old_snapshot.generation
    del store.data[999]
    assert builder.publish(plan.build_id).generation == old_snapshot.generation + 1


@pytest.mark.mysql
def test_real_standalone_build_and_publish(test_database, tmp_path):
    from uuid import uuid4
    from pymilvus import MilvusClient
    from retailq.knowledge.milvus_store import MilvusKnowledgeStore
    from tests.support.milvus import require_isolated_milvus
    uri = 'http://127.0.0.1:19531'
    build_id = uuid4().hex
    collection = 'test_' + build_id
    require_isolated_milvus(uri, collection)
    root = tmp_path / 'knowledge'
    coordinator = Coordinator(tmp_path / 'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', uri, root))
    corpus = CorpusStore(root, coordinator)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'policy.md').write_text('# 政策\n## 运费\n未满收10元。\n', encoding='utf-8')
    manifest = source / 'manifest.json'
    manifest.write_text(json.dumps({'files': [{'path': 'policy.md', 'content_type': 'policy'}]}), encoding='utf-8')
    repository = KnowledgeRepository(test_database)
    importer = CorpusImporter(repository, corpus, TestBudget(), collection_prefix='test')
    plan = importer.prepare(manifest, operation_id=build_id)
    class Embedding:
        def embed(self, texts): return [[1.] + [0.] * 1023 for _ in texts]
    builder = IndexBuilder(repository, corpus, importer, coordinator, Embedding(),
                           lambda name: MilvusKnowledgeStore(uri, {'model': 'test'}, collection=name))
    try:
        report = builder.build(plan, stop=Event())
        assert report.phase == 'verified' and report.expected == report.indexed == 1
        snapshot = builder.publish(build_id)
        assert snapshot.generation == 1
        from retailq.knowledge.service import KnowledgeService
        vectors = MilvusKnowledgeStore(uri, {'model': 'test'}, collection=collection)
        try:
            repository.reset_pending(list(snapshot.active_ids))
            service = KnowledgeService(repository, Embedding(), vectors, TestBudget(), corpus=corpus)
            restored = service.vectorize_pending()
            assert restored.failed == restored.pending_after == 0
            assert restored.done == 1
        finally:
            vectors.close()
    finally:
        client = MilvusClient(uri)
        require_isolated_milvus(uri, collection)
        if client.has_collection(collection): client.drop_collection(collection)
        client.close()


@pytest.mark.mysql
@pytest.mark.standalone
def test_publish_approved_requires_receipt_and_is_idempotent(review_harness, tmp_path):
    from retailq.knowledge.corpus_import import ReviewApprovalResolver
    from tests.support.reviews import prepared_new_group, queued_approval, make_application, confirmed_target

    harness = review_harness
    run_id, group_id = prepared_new_group(harness)
    operation = queued_approval(harness, run_id, group_id)
    make_application(harness).decide(operation.operation_id, stop=Event())
    target = confirmed_target(harness, run_id, group_id)
    root = tmp_path / 'knowledge'
    coordinator = Coordinator(tmp_path / 'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root))
    corpus = CorpusStore(root, coordinator)
    old = CorpusPlan('a'*32, 0, 'knowledge_a', 'b'*64, (), (), (), (), 'prepared')
    corpus.stage(old)
    for phase, next_phase in [('prepared', 'sql_committed'), ('sql_committed', 'indexing'), ('indexing', 'verified')]:
        old = replace(old, phase=next_phase)
        corpus.update(old, expected_phase=phase)
    corpus.publish(old.build_id, expected_generation=0)
    repository = KnowledgeRepository(harness.database)
    importer = CorpusImporter(repository, corpus, TestBudget(), approval_resolver=ReviewApprovalResolver(harness.store, harness.repository))
    class Embedding:
        def embed(self, texts): return [[1.] + [0.] * 1023 for _ in texts]
    collections = {}
    class Store:
        identity = 'c'*64
        def __init__(self, name): self.collection = name; self.data = collections.setdefault(name, {})
        def exists(self): return True
        def ensure_collection(self): pass
        def fingerprints(self, ids): return {i:self.data[i] for i in ids if i in self.data}
        def upsert(self, items): self.data.update((i.id, i.fingerprint) for i in items); return [i.id for i in items]
        def count(self): return len(self.data)
        def close(self): pass
    builder = IndexBuilder(repository, corpus, importer, coordinator, Embedding(), Store)
    with pytest.raises(ServiceError):
        builder.publish_approved((target + 100,), operation_id='unapproved')
    builder.publish_approved((target,), operation_id='approved')
    assert corpus.active().generation == 2 and target in corpus.active().approved_ids
    builder.publish_approved((target,), operation_id='approved')
    assert corpus.active().generation == 2


def test_status_distinguishes_historical_done_from_active():
    from retailq.knowledge.contracts import ChunkDraft, ChunkRecord
    from retailq.knowledge.corpus import CorpusSnapshot
    from retailq.knowledge.service import KnowledgeService
    from retailq.knowledge.text import content_fingerprint
    draft = ChunkDraft('分类', '问题', '答案', None, 'faq')
    rows = [ChunkRecord(1, draft, None, None, '1', 'done'), ChunkRecord(2, draft, None, None, '2', 'done')]
    snapshot = CorpusSnapshot(1, 'c', 'x', (), (1,), (2,), ((1, content_fingerprint(draft)),), 'x', (1,), 'a'*32)
    class Repository:
        def all_chunks(self): return rows
    class Corpus:
        def active(self): return snapshot
        def _path(self, *args):
            from types import SimpleNamespace
            return SimpleNamespace(exists=lambda: True)
    service = KnowledgeService(Repository(), None, None, None, corpus=Corpus())
    status = service.status()
    assert status['done'] == 2 and status['active'] == 1 and status['history'] == 1


@pytest.mark.mysql
@pytest.mark.standalone
def test_review_application_invokes_publication_only_after_receipt(review_harness):
    from tests.support.reviews import prepared_new_group, queued_approval, make_application, confirmed_target
    harness = review_harness
    run_id, group_id = prepared_new_group(harness)
    operation = queued_approval(harness, run_id, group_id)
    application = make_application(harness)
    calls = []
    class Builder:
        def publish_approved(self, ids, *, operation_id):
            group = harness.store.get_group(run_id, group_id)
            assert harness.repository.inspect_application(group) == 'committed'
            calls.append((ids, operation_id))
    application.service.index_builder = Builder()
    application.decide(operation.operation_id, stop=Event())
    assert calls == [((confirmed_target(harness, run_id, group_id),), operation.operation_id)]
