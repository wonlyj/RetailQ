"""Read-only, safe projections. Prepared and historical guesses are never links."""
import json
from pathlib import Path
import re

from sqlalchemy import select

from retailq.errors import ServiceError
from retailq.knowledge.contracts import ConversationSnapshot, MiningMessage
from retailq.knowledge.models import QAStaging
from retailq.knowledge.review_contracts import (
    AuditFault, ChunkDetail, ConsoleSummary, GroupDetail, GroupSummary, LegacyRunDetail, LegacyTarget,
    OperationView, RunCounters, RunDetail, RunSummary, SafeFailure,
)
from retailq.knowledge.review_repository import paginate
from retailq.knowledge.review_store import review_error
from retailq.knowledge.text import normalize_text
from retailq.repositories import database_errors


class ReviewQueries:
    def __init__(self, repository, knowledge, store, legacy_root: Path):
        self.repository, self.knowledge, self.store = repository, knowledge, store
        self.legacy_root = Path(legacy_root).resolve()

    def sources(self, **kwargs):
        return self.repository.list_sources(**kwargs)

    def source(self, cid):
        return self.repository.source_detail(cid)

    def categories(self, **kwargs):
        return self.repository.categories(**kwargs)

    def _run_summary(self, run, _attempt=0):
        sealed = self.store.get_seal(run.run_id)
        if isinstance(run, AuditFault):
            return RunSummary(run_id=run.run_id, kind='review', phase='sealed' if sealed else 'conflict',
                counters=None, created_at=None, error=SafeFailure(code=run.code, message=run.message))
        try:
            # Counts are projections from groups and authoritative SQL, never a
            # cached run counter that could have missed a post-commit crash.
            groups = [self.group(run.run_id, gid) for gid in run.group_ids]
            pending = sum(g.summary.decision == 'pending' for g in groups)
            approved = [g for g in groups if g.summary.decision == 'approved']
            applied = [g for g in approved if g.target_confirmed]
            requested, confirmed, pending_vectors = set(), set(), set()
            for g in applied:
                record = self.store.get_group(run.run_id, g.summary.group_id)
                app = record.application
                requested.update(app.vector_requested_ids)
                if g.vectorize_status == 'done':
                    confirmed.update(app.vector_confirmed_ids)
                else:
                    pending_vectors.add(g.target_chunk_id)
            staged = self.knowledge.staging([b.batch_no for b in run.batches])
            counters = RunCounters(extracted=len(staged), review_pending=pending, approved=len(approved),
                rejected=sum(g.summary.decision == 'rejected' for g in groups), applied=len(applied),
                vectorized=len(requested & confirmed), pending_vectors=len(pending_vectors))
            error = run.error or next((g.error for g in groups if g.error), None)
        except ServiceError as exc:
            counters = None
            error = SafeFailure(code=exc.code, message=exc.public_message)
        current = self.store.get_run(run.run_id)
        if current.revision != run.revision:
            if _attempt == 0:
                return self._run_summary(current, _attempt=1)
            counters = None
            error = SafeFailure(code='knowledge_review_processing', message='任务状态正在变化，请刷新。')
        return RunSummary(run_id=run.run_id, kind='review', phase='sealed' if sealed else run.phase,
                           counters=counters, created_at=run.created_at, error=error)

    def summary(self):
        current = self.store.unfinished()
        item = self._run_summary(current) if current else None
        return ConsoleSummary(chunks=len(self.knowledge.all_chunks()),
            counters=item.counters if item else None, current_run_id=item.run_id if item else None,
            phase=item.phase if item else None, error=item.error if item else None)

    def runs(self, *, offset=0, limit=20):
        items = [self._run_summary(run) for run in self.store.list_runs()]
        for path in sorted(self.legacy_root.glob('run-*.json')):
            rid = path.stem.removeprefix('run-')
            self.store._id(rid)
            try:
                legacy = self.legacy_run(rid)
                error, phase = None, legacy.phase
            except ServiceError:
                phase = 'unknown'
                error = SafeFailure(code='knowledge_review_corrupt', message='旧版任务记录损坏或无法读取。')
            items.append(RunSummary(run_id=rid, kind='legacy', phase=phase, counters=None, created_at=None, error=error))
        items.sort(key=lambda r: (r.created_at or '', r.run_id), reverse=True)
        return paginate(items, offset, limit)

    def run(self, run_id):
        self.store._id(run_id)
        run = next((r for r in self.store.list_runs() if r.run_id == run_id), None)
        if run is None:
            raise review_error('knowledge_review_not_found', '审核任务不存在。', 404)
        summary = self._run_summary(run)
        if isinstance(run, AuditFault):
            return RunDetail(summary=summary, sources=None, groups_count=None)
        return RunDetail(summary=summary, sources=run.sources, groups_count=len(run.group_ids),
                          completed_batches=len(run.completed_batches), total_batches=len(run.batches))

    def group(self, run_id, group_id):
        run = self.store.get_run(run_id)
        if group_id not in run.group_ids:
            raise review_error('knowledge_review_not_found', '该任务没有此审核组。', 404)
        for attempt in range(2):
            group = self.store.get_group(run_id, group_id)
            result = self._group_detail(group)
            if self.store.get_group(run_id, group_id).revision == group.revision:
                return result
        raise review_error('knowledge_review_processing', '审核组正在变化，请刷新。')

    def _group_detail(self, group):
        run_id, group_id = group.run_id, group.group_id
        app, decision = group.application, group.decision
        disposition = ('rejected' if decision and decision.kind == 'rejected' else
                       'merge' if group.target_chunk_id is not None else 'new')
        category = group.suggestion.category if group.suggestion else None
        error = app.error if app else None
        target, confirmed, vector_status = None, False, None
        planned = app.prepared.chunk_id if app else group.target_chunk_id
        if decision and decision.kind == 'rejected':
            category, planned = None, None
        elif decision and group.target_chunk_id is None:
            category = decision.category
        if group.target_chunk_id is not None and disposition != 'rejected':
            existing = self.knowledge.get_chunks([group.target_chunk_id]).get(group.target_chunk_id)
            category = existing.draft.category if existing else None
        if app and app.phase == 'applied':
            if self.repository.inspect_application(group) == 'committed':
                target = app.prepared.chunk_id
                confirmed = target is not None
                if target is not None:
                    vector_status = self.knowledge.get_chunks([target])[target].vectorize_status
            else:
                error = SafeFailure(code='knowledge_review_conflict', message='当前原文或暂存状态与审核确证不一致。')
        summary = GroupSummary(group_id=group_id, revision=group.revision, question='\n'.join(group.questions),
            disposition=disposition, decision=decision.kind if decision else 'pending', category=category,
            suggestion=group.suggestion)
        return GroupDetail(summary=summary, members=group.members, questions=group.questions, answer=group.answer,
            dispositions=app.prepared.members if app and app.phase == 'applied' and error is None else (),
            target_confirmed=confirmed, planned_target_id=planned if not confirmed else None,
            target_chunk_id=target if confirmed else None, application_phase=app.phase if app else None,
            vectorize_status=vector_status, error=error)

    def groups(self, run_id, *, query='', disposition='all', category='', offset=0, limit=20):
        if disposition not in ('all', 'new', 'merge', 'rejected', 'pending'):
            raise ValueError('invalid disposition filter')
        run = self.store.get_run(run_id)
        items = []
        for gid in run.group_ids:
            detail = self.group(run_id, gid)
            item = detail.summary
            if disposition == 'pending' and item.decision != 'pending':
                continue
            if disposition not in ('all', 'pending') and item.disposition != disposition:
                continue
            if category and item.category != category:
                continue
            if query and query not in item.question and query not in detail.answer and not any(
                    query in m.source_ref or query in str(m.staging_id) for m in detail.members):
                continue
            items.append(item)
        return paginate(items, offset, limit)

    def chunk(self, cid):
        row = self.knowledge.get_chunks([cid]).get(cid)
        if row is None:
            raise ServiceError('knowledge_chunk_not_found', 404, '知识块不存在。')
        return ChunkDetail(record=row)

    def operation(self, operation_id):
        operation = self.store.get_operation(operation_id)
        phase, error = operation.phase, operation.error
        if operation.reused_operation_id:
            original = self.store.get_operation(operation.reused_operation_id)
            if original.run_id != operation.run_id or original.kind != operation.kind or original.reused_operation_id:
                raise review_error('knowledge_review_corrupt', '操作复用记录不一致。')
            phase, error = original.phase, original.error
        if self.store.get_seal(operation.run_id):
            phase = 'sealed'
        return OperationView(operation_id=operation.operation_id, run_id=operation.run_id,
                              kind=operation.kind, phase=phase, error=error)

    @database_errors
    def legacy_run(self, run_id):
        self.store._id(run_id)
        path = self.legacy_root / f'run-{run_id}.json'
        if not path.resolve().is_relative_to(self.legacy_root):
            raise review_error('knowledge_review_corrupt', '旧版记录路径无效。')
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if data['run_id'] != run_id:
                raise ValueError('legacy identity mismatch')
            sources = tuple(ConversationSnapshot(s['source_ref'], s['conversation_id'], s['max_message_id'],
                tuple(MiningMessage(**m) for m in s['messages'])) for s in data['sources'])
            groups = data.get('dedup_groups', [])
            explicit = []
            with self.knowledge.database.sessions() as session:
                for index, group in enumerate(groups):
                    cid = group.get('target_chunk_id')
                    if type(cid) is not int or not 1 <= cid <= 2**63 - 1:
                        continue
                    row = self.knowledge.get_chunks([cid]).get(cid)
                    members = session.scalars(select(QAStaging).where(QAStaging.id.in_(group['member_ids']))).all()
                    # An explicit ID is necessary; complete original evidence must
                    # also agree. Missing evidence remains unknown, never guessed.
                    if row and members and {m.id for m in members} == set(group['member_ids']) and all(
                            normalize_text(m.answer) == normalize_text(row.draft.answer) for m in members):
                        explicit.append(LegacyTarget(group_index=index, chunk_id=cid))
            return LegacyRunDetail(run_id=run_id, phase=str(data['phase']), sources=sources,
                explicit_targets=tuple(explicit), missing_provenance=len(groups) - len(explicit))
        except FileNotFoundError:
            raise review_error('knowledge_review_not_found', '旧版任务不存在。', 404) from None
        except (OSError, ValueError, TypeError, KeyError):
            raise review_error('knowledge_review_corrupt', '旧版任务记录损坏或无法核对。') from None
