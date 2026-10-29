"""Build immutable collection generations and publish only verified snapshots."""
from dataclasses import dataclass, replace
from contextlib import nullcontext
from hashlib import sha256
from threading import Event

from retailq.errors import ServiceError
from retailq.knowledge.corpus import CorpusPlan, is_active
from retailq.knowledge.retrieval_contracts import IndexedChunk
from retailq.knowledge.text import content_fingerprint, embedding_text


@dataclass(frozen=True)
class BuildReport:
    build_id: str
    phase: str
    expected: int
    indexed: int
    failed: int
    pending: int
    identity_hash: str


class IndexBuilder:
    def __init__(self, repository, corpus, importer, coordinator, embedding, store_factory):
        self.repository, self.corpus, self.importer = repository, corpus, importer
        self.coordinator, self.embedding, self.store_factory = coordinator, embedding, store_factory

    def _lease(self, held=False):
        if held:
            return nullcontext()
        lease = self.coordinator.try_acquire('maintenance')
        if lease is None:
            raise ServiceError('knowledge_maintenance_busy', 409, '知识库维护任务正在运行。')
        return lease

    def _rows(self, plan):
        rows = self.repository.get_chunks(list(plan.active_ids))
        if set(rows) != set(plan.active_ids) or any(not is_active(rows[i], plan) for i in plan.active_ids):
            raise ServiceError('knowledge_corpus_invalid', 409, '语料与数据库不一致。')
        return rows

    def build(self, plan, *, stop, _held=False):
        with self._lease(_held):
            plan = self.importer.resume(plan.build_id)
            if plan.phase not in ('sql_committed', 'indexing', 'verified'):
                raise ServiceError('knowledge_corpus_invalid', 409, '语料构建阶段不允许索引。')
            rows = self._rows(plan)
            store = self.store_factory(plan.collection)
            try:
                if plan.phase != 'sql_committed' and not store.exists():
                    raise ServiceError('knowledge_corpus_invalid', 409, '已有构建的集合不存在，请核查。')
                store.ensure_collection()
                if plan.identity_hash and plan.identity_hash != store.identity:
                    raise ServiceError('knowledge_identity_mismatch', 409, '集合模型身份不一致。')
                if plan.phase == 'sql_committed':
                    updated = replace(plan, identity_hash=store.identity, phase='indexing')
                    self.corpus.update(updated, expected_phase='sql_committed')
                    plan = updated
                expected = len(rows)
                indexed, failed = 0, 0
                fingerprints = store.fingerprints(list(plan.active_ids))
                for item_id in plan.active_ids:
                    if stop.is_set():
                        break
                    row = rows[item_id]
                    fingerprint = content_fingerprint(row.draft)
                    try:
                        if fingerprints.get(item_id) != fingerprint:
                            vector = self.embedding.embed([embedding_text(row.draft)])[0]
                            source = next((s for s in plan.sources if item_id in s.chunk_ids), None)
                            item = IndexedChunk(item_id, vector, fingerprint, embedding_text(row.draft), row.draft.category,
                                                row.draft.content_type or 'faq', source.key if source else 'review',
                                                source.digest if source else fingerprint)
                            if store.upsert([item]) != [item_id]:
                                raise ValueError('upsert receipt mismatch')
                        if store.fingerprints([item_id]).get(item_id) != fingerprint:
                            raise ValueError('index fingerprint mismatch')
                        if not self.repository.mark_done(item_id, str(item_id), fingerprint):
                            raise ValueError('SQL fingerprint mismatch')
                        indexed += 1
                    except Exception:
                        failed += 1
                pending = expected - indexed
                if not failed and not pending and plan.phase == 'indexing':
                    if store.count() != expected:
                        raise ServiceError('knowledge_corpus_invalid', 409, '集合数量与清单不一致。')
                    plan = replace(plan, phase='verified')
                    self.corpus.update(plan, expected_phase='indexing')
                return BuildReport(plan.build_id, plan.phase, expected, indexed, failed, pending, plan.identity_hash)
            finally:
                store.close()

    def retry(self, build_id, *, stop):
        return self.build(self.corpus.get_plan(build_id), stop=stop)

    def publish(self, build_id, *, _held=False):
        with self._lease(_held):
            plan = self.importer.resume(build_id)
            if plan.phase != 'verified':
                raise ServiceError('knowledge_corpus_invalid', 409, '语料尚未验证完成。')
            rows = self._rows(plan)
            store = self.store_factory(plan.collection)
            try:
                if not store.exists():
                    raise ServiceError('knowledge_corpus_invalid', 409, '语料集合不存在。')
                store.ensure_collection()
                if store.identity != plan.identity_hash or store.count() != len(rows):
                    raise ServiceError('knowledge_corpus_invalid', 409, '集合身份或数量不一致。')
                if store.fingerprints(list(rows)) != dict(plan.fingerprints):
                    raise ServiceError('knowledge_corpus_invalid', 409, '集合指纹与清单不一致。')
                if any(row.vectorize_status != 'done' or row.vector_id != str(row.id) for row in rows.values()):
                    raise ServiceError('knowledge_corpus_invalid', 409, '知识向量尚未确认。')
                return self.corpus.publish(build_id, expected_generation=plan.previous_generation)
            finally:
                store.close()

    def publish_approved(self, ids, *, operation_id, _held=False):
        if not isinstance(operation_id, str) or not operation_id or not ids or len(set(ids)) != len(ids):
            raise ValueError('invalid approval operation')
        resolver = self.importer.approval_resolver
        if resolver is None or not set(ids) <= set(resolver()):
            raise ServiceError('knowledge_review_conflict', 409, '批准知识缺少已确认的审核回执。')
        build_id = sha256(('review-publication\0' + operation_id).encode('utf-8')).hexdigest()[:32]
        with self._lease(_held):
            snapshot = self.corpus.active()
            if set(ids) & set(snapshot.retired_ids):
                raise ServiceError('knowledge_review_conflict', 409, '退役知识不能重新发布。')
            if set(ids) <= set(snapshot.active_ids):
                rows = self.repository.get_chunks(list(ids))
                if set(rows) != set(ids) or any(not is_active(rows[i], snapshot) for i in ids):
                    raise ServiceError('knowledge_review_conflict', 409, '已发布知识与审核回执不一致。')
                return
            ids = tuple(i for i in ids if i not in snapshot.active_ids)
            path = self.corpus._path('builds', build_id + '.json')
            if not path.exists():
                rows = self.repository.get_chunks(list(ids))
                if set(rows) != set(ids):
                    raise ServiceError('knowledge_review_conflict', 409, '批准知识不存在。')
                approved = tuple(sorted(set(snapshot.approved_ids) | set(ids)))
                fingerprints = dict(snapshot.fingerprints)
                fingerprints.update((item_id, content_fingerprint(rows[item_id].draft)) for item_id in ids)
                prefix = snapshot.collection.rsplit('_', 1)[0]
                plan = CorpusPlan(build_id, snapshot.generation, prefix + '_' + build_id, '', snapshot.sources,
                                  tuple(sorted(set(snapshot.active_ids) | set(ids))), snapshot.retired_ids,
                                  tuple(sorted(fingerprints.items())), 'prepared', approved)
                self.corpus.stage(plan)
            else:
                plan = self.corpus.get_plan(build_id)
                if not set(ids) <= set(plan.approved_ids):
                    raise ServiceError('knowledge_review_conflict', 409, '审核操作与既有发布记录不一致。')
            plan = self.importer.resume(build_id)
        report = self.build(plan, stop=Event(), _held=_held)
        if report.failed or report.pending:
            raise ServiceError('knowledge_vectorize_incomplete', 503, '审核知识已保留，检索发布尚未完成。')
        self.publish(build_id, _held=_held)
