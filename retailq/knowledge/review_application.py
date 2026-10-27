"""Apply durable human decisions, confirm SQL evidence, then publish scoped vectors."""
from retailq.errors import ServiceError
from retailq.knowledge.contracts import ChunkDraft
from retailq.knowledge.review_classification import check_stop
from retailq.knowledge.review_contracts import (
    ApplicationRecord, DecisionCommand, ReviewDecision, RunCounters, SafeFailure, SealCommand,
    evolve, normalize_category,
)
from retailq.knowledge.review_store import review_error
from retailq.knowledge.text import content_fingerprint, embedding_text


class ReviewApplication:
    def __init__(self, knowledge, repository, store, service):
        self.knowledge, self.repository, self.store, self.service = knowledge, repository, store, service

    def _run(self, run_id):
        if self.store.get_seal(run_id):
            raise review_error('knowledge_review_sealed', '任务已终止并封存。')
        return self.store.get_run(run_id)

    def _save_run(self, run, **changes):
        return self.store.put_run(evolve(run, updated_at=self.store.timestamp(), **changes),
                                   expected_revision=run.revision)

    def _save_group(self, group, **changes):
        return self.store.put_group(evolve(group, **changes), expected_revision=group.revision)

    def _record_failure(self, run_id, exc):
        error = SafeFailure(code=exc.code if isinstance(exc, ServiceError) else 'knowledge_review_incomplete',
                            message=exc.public_message if isinstance(exc, ServiceError) else '审核应用未确认完成，请继续任务核查。')
        try:
            run = self._run(run_id)
            self._save_run(run, phase='conflict' if error.code == 'knowledge_review_conflict' else 'failed', error=error)
        except (OSError, ServiceError):
            pass

    def _validated_decision(self, command, group):
        category = None
        if command.decision == 'approve':
            if group.target_chunk_id is None:
                try:
                    category = normalize_category(command.category)
                    draft = ChunkDraft(category, '\n'.join(group.questions), group.answer, None, 'faq')
                    self.service.budget.validate(embedding_text(draft))
                except (ValueError, TypeError):
                    raise ServiceError('knowledge_review_category_invalid', 422,
                                       '最终分类或完整知识超过限制，请更正分类或拒绝该组。') from None
            elif command.category is not None:
                raise ServiceError('knowledge_review_category_invalid', 422, '归并操作不能修改已有分类。')
        elif command.category is not None:
            raise ServiceError('knowledge_review_category_invalid', 422, '拒绝操作不能提交分类。')
        return ReviewDecision(operation_id=command.request_key,
            kind='approved' if command.decision == 'approve' else 'rejected', category=category,
            reason=command.reason, decided_at=self.store.timestamp())

    def decide(self, operation_id, *, stop):
        operation = self.store.get_operation(operation_id)
        command = operation.command
        if not isinstance(command, DecisionCommand):
            raise review_error(message='操作类型不是审核决定。')
        self._run(command.run_id)
        group = self.store.get_group(command.run_id, command.group_id)
        decision = self._validated_decision(command, group)
        if group.decision:
            old = group.decision
            if (old.kind, old.category, old.reason) != (decision.kind, decision.category, decision.reason):
                raise review_error(message='该组已有不同决定，不能覆盖。')
        else:
            if group.revision != command.expected_revision:
                raise review_error('knowledge_review_stale', '审核组已变化，请刷新后重新选择。')
            check_stop(stop)
            group = self._save_group(group, decision=decision)
        try:
            self._apply(group, stop)
            return self.retry_vectors(command.run_id, stop=stop)
        except Exception as exc:
            self._record_failure(command.run_id, exc)
            raise

    def _apply(self, group, stop):
        self._run(group.run_id)
        check_stop(stop)
        if group.application:
            state = self.repository.inspect_application(group)
            if state == 'conflict' or (group.application.phase == 'applied' and state != 'committed'):
                error = SafeFailure(code='knowledge_review_conflict', message='暂存状态或目标原文发生变化，无法确证。')
                self._save_group(group, application=evolve(group.application, phase='conflict', error=error))
                raise review_error()
            if state == 'committed':
                if group.application.phase != 'applied':
                    return self._save_group(group, application=evolve(group.application, phase='applied', error=None))
                return group
        # A prepared-but-rolled-back transaction can receive a new auto ID. The
        # earlier planned ID must never be presented as a confirmed destination.
        def prepare(prepared):
            nonlocal group
            group = self._save_group(group, application=ApplicationRecord(phase='prepared', prepared=prepared))
        self.repository.apply_group(group, group.decision, prepare)
        return self._save_group(group, application=evolve(group.application, phase='applied', error=None))

    def resume_applications(self, run_id, *, stop):
        run = self._run(run_id)
        try:
            for gid in run.group_ids:
                check_stop(stop)
                group = self.store.get_group(run_id, gid)
                if group.decision:
                    self._apply(group, stop)
            return self.retry_vectors(run_id, stop=stop)
        except Exception as exc:
            self._record_failure(run_id, exc)
            raise

    def retry_vectors(self, run_id, *, stop):
        run = self._run(run_id)
        ids = set()
        for gid in run.group_ids:
            group = self.store.get_group(run_id, gid)
            if not group.decision or group.decision.kind != 'approved' or not group.application:
                continue
            if group.application.phase != 'applied':
                continue
            if self.repository.inspect_application(group) != 'committed':
                raise review_error()
            prepared = group.application.prepared
            row = self.knowledge.get_chunks([prepared.chunk_id])[prepared.chunk_id]
            app = group.application
            if row.vectorize_status == 'pending':
                if row.id not in app.vector_requested_ids:
                    group = self._save_group(group, application=evolve(app,
                        vector_requested_ids=tuple(sorted(set(app.vector_requested_ids) | {row.id}))))
                ids.add(row.id)
        report = self.service.vectorize_pending(stop=stop, only_ids=tuple(sorted(ids)))
        for gid in run.group_ids:
            group = self.store.get_group(run_id, gid)
            app = group.application
            if not app or app.phase != 'applied' or not app.vector_requested_ids:
                continue
            rows = self.knowledge.get_chunks(list(app.vector_requested_ids))
            confirmed = tuple(sorted(cid for cid, row in rows.items() if
                row.vectorize_status == 'done' and row.vector_id == str(cid) and
                content_fingerprint(row.draft) == app.prepared.fingerprint))
            if confirmed != app.vector_confirmed_ids:
                self._save_group(group, application=evolve(app, vector_confirmed_ids=confirmed))
        result = self.refresh_phase(run_id)
        if report.failed or report.pending_after:
            error = ServiceError('knowledge_vectorize_incomplete', 503, '已批准原文保留，关联知识尚待补齐检索向量。')
            self._record_failure(run_id, error)
            raise error
        if stop.is_set():
            check_stop(stop)
        return result

    def refresh_phase(self, run_id):
        run = self.store.get_run(run_id)
        if run.phase == 'sealed':
            return run
        groups = [self.store.get_group(run_id, gid) for gid in run.group_ids]
        approved = [g for g in groups if g.decision and g.decision.kind == 'approved']
        rejected = [g for g in groups if g.decision and g.decision.kind == 'rejected']
        pending = len(groups) - len(approved) - len(rejected)
        applied, requested, confirmed, pending_ids = 0, set(), set(), set()
        conflict = unresolved = False
        for group in groups:
            if not group.decision:
                continue
            app = group.application
            if app is None or app.phase == 'prepared':
                unresolved = True
                continue
            if app.phase == 'conflict' or self.repository.inspect_application(group) != 'committed':
                conflict = True
                continue
            if group.decision.kind == 'approved':
                applied += 1
                cid = app.prepared.chunk_id
                row = self.knowledge.get_chunks([cid])[cid]
                if row.vectorize_status != 'done' or row.vector_id != str(cid):
                    pending_ids.add(cid)
                else:
                    confirmed.update(app.vector_confirmed_ids)
                requested.update(app.vector_requested_ids)
        counters = RunCounters(extracted=run.counters.extracted, review_pending=pending,
            approved=len(approved), rejected=len(rejected), applied=applied,
            vectorized=len(confirmed & requested), pending_vectors=len(pending_ids))
        phase = ('conflict' if conflict else 'applying' if unresolved else 'waiting_review' if pending else
                 'vectorize' if pending_ids else 'completed')
        return self._save_run(run, phase=phase, counters=counters, error=None)

    def seal(self, operation_id):
        command = self.store.get_operation(operation_id).command
        if not isinstance(command, SealCommand):
            raise review_error(message='操作类型不是终止并封存。')
        return self.store.seal(command.run_id, operation_id, command.reason or '人工终止并封存')
