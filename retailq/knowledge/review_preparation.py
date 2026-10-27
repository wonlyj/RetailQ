"""Extract, deduplicate and suggest categories, then wait for human review."""
import hashlib
import json
import re

from retailq.errors import ServiceError
from retailq.knowledge.dedup import global_dedup
from retailq.knowledge.extraction import batch_conversations, extract_to_staging
from retailq.knowledge.qa_gateway import prompt
from retailq.knowledge.review_classification import check_stop, classify_groups
from retailq.knowledge.review_contracts import (
    AuditFault, BatchRecord, ReviewGroup, RunCounters, SafeFailure, SourceQA, evolve,
)
from retailq.knowledge.review_store import review_error
from retailq.knowledge.text import content_fingerprint, normalize_text


def group_id(run_id, ids):
    return hashlib.sha256((run_id + ':' + ','.join(str(i) for i in sorted(ids))).encode()).hexdigest()[:32]


class ReviewPreparation:
    def __init__(self, knowledge, repository, store, gateway, embedding, *, input_budget,
                 max_conversations=20, min_score=.85, candidate_k=8):
        self.knowledge, self.repository, self.store = knowledge, repository, store
        self.gateway, self.embedding = gateway, embedding
        self.input_budget, self.max_conversations = input_budget, max_conversations
        self.min_score, self.candidate_k = min_score, candidate_k

    def rule_hash(self):
        config = json.dumps({'schema':2, 'algorithm':'review-v2-plan-1', 'budget':self.input_budget,
                             'batch':self.max_conversations, 'score':self.min_score,
                             'candidate_k':self.candidate_k}, sort_keys=True)
        return hashlib.sha256((prompt('qa_extract.txt') + prompt('qa_dedup.txt') +
                               prompt('qa_category.txt') + config).encode('utf-8')).hexdigest()

    def _save(self, run, **changes):
        return self.store.put_run(evolve(run, updated_at=self.store.timestamp(), **changes),
                                   expected_revision=run.revision)

    @staticmethod
    def _source_qa(row, sources):
        source = next((s for s in sources if s.source_ref == row.source_ref), None)
        if source is None:
            raise review_error(message='暂存来源未出现在冻结批次中。')
        ids = []
        has_user = has_answer = False
        for m in source.messages:
            if m.role == 'user' and row.question in (m.content or ''):
                ids.append(m.id)
                has_user = True
            elif m.role == 'assistant':
                text = normalize_text(m.content or '')
                if row.answer in (text, *re.split(r'\n\s*\n', text)):
                    ids.append(m.id)
                    has_answer = True
        if not has_user or not has_answer:
            raise review_error(message='暂存问答缺少完整来源证据。')
        return SourceQA(staging_id=row.id, batch_no=row.batch_no, source_ref=row.source_ref,
                         question=row.question, answer=row.answer, message_ids=tuple(ids))

    def prepare(self, run_id, *, stop):
        run = self.store.get_run(run_id)
        if run.phase == 'sealed':
            raise review_error('knowledge_review_sealed', '任务已终止并封存。')
        if run.rule_hash != self.rule_hash():
            raise review_error('knowledge_run_version_mismatch', '准备规则已变化，请核查原任务。')
        if run.phase in ('completed', 'waiting_review'):
            return run
        try:
            check_stop(stop)
            if not run.batches:
                batches = batch_conversations(list(run.sources), max_conversations=self.max_conversations,
                                               input_budget=self.input_budget)
                records = tuple(BatchRecord(batch_no=hashlib.sha256(
                    f'review-v2:{run_id}:{i}'.encode()).hexdigest(), sources=tuple(b)) for i, b in enumerate(batches))
                run = self._save(run, batches=records, phase='extraction', error=None)
            for batch in run.batches:
                check_stop(stop)
                if batch.batch_no in run.completed_batches:
                    continue
                extract_to_staging(self.knowledge, self.gateway, list(batch.sources), batch.batch_no)
                run = self._save(run, phase='extraction', completed_batches=run.completed_batches + (batch.batch_no,))
            staged = self.knowledge.staging([b.batch_no for b in run.batches])
            if run.group_plan is None:
                run = self._save(run, phase='dedup')
                check_stop(stop)
                existing = self.knowledge.all_chunks()
                groups = global_dedup(staged, existing, self.embedding, self.gateway,
                                      min_score=self.min_score, candidate_k=self.candidate_k, stop=stop)
                by_id = {s.id:s for s in staged}
                targets = {r.id:r for r in existing}
                fragments = [s for b in run.batches for s in b.sources]
                planned = []
                for group in groups:
                    members = tuple(self._source_qa(by_id[mid], fragments) for mid in group.member_ids)
                    target = targets.get(group.target_chunk_id)
                    planned.append(ReviewGroup(run_id=run_id, group_id=group_id(run_id, group.member_ids),
                        members=members, representative_id=group.representative_id, target_chunk_id=group.target_chunk_id,
                        target_fingerprint=content_fingerprint(target.draft) if target else None,
                        questions=group.questions, answer=by_id[group.representative_id or group.member_ids[0]].answer))
                # Durable dedup intent before splitting into group records.
                run = self._save(run, group_plan=tuple(planned), phase='classification')
            for planned in run.group_plan:
                check_stop(stop)
                try:
                    self.store.get_group(run_id, planned.group_id)
                except ServiceError as exc:
                    if exc.status_code != 404:
                        raise
                    self.store.put_group(planned, expected_revision=None)
            run = self._save(run, group_ids=tuple(g.group_id for g in run.group_plan),
                             phase='classification', error=None)
            catalog = []
            offset = 0
            while True:
                page = self.repository.categories(offset=offset, limit=100)
                catalog.extend(page.items)
                offset += len(page.items)
                if offset >= page.total:
                    break
            for gid in run.group_ids:
                check_stop(stop)
                group = self.store.get_group(run_id, gid)
                if group.suggestion is not None:
                    continue
                suggestion = classify_groups((group,), tuple(catalog), self.gateway,
                                              input_budget=self.input_budget, stop=stop)[0]
                self.store.put_group(evolve(group, suggestion=suggestion), expected_revision=group.revision)
            return self._save(run, phase='waiting_review' if run.group_ids else 'completed', error=None,
                              counters=RunCounters(extracted=len(staged), review_pending=len(run.group_ids)))
        except Exception as exc:
            failure = SafeFailure(code=exc.code if isinstance(exc, ServiceError) else 'knowledge_preparation_failed',
                                   message=exc.public_message if isinstance(exc, ServiceError) else '准备任务未完成，保留进度供继续。')
            # Do not obscure the original disk failure if recording it also fails.
            try:
                self._save(self.store.get_run(run_id), phase='interrupted' if stop.is_set() else 'failed', error=failure)
            except (OSError, ServiceError):
                pass
            raise


def processed_sources(store):
    processed = set()
    for run in store.list_runs():
        if isinstance(run, AuditFault) or run.group_plan is None:
            continue
        for source in run.sources:
            fragments = [b for b in run.batches if any(s.conversation_id == source.conversation_id for s in b.sources)]
            if not all(b.batch_no in run.completed_batches for b in fragments):
                continue
            members = []
            certain = True
            for planned in run.group_plan:
                if any(m.source_ref == source.source_ref or m.source_ref.startswith(source.source_ref + '#part')
                       for m in planned.members):
                    try:
                        group = store.get_group(run.run_id, planned.group_id)
                        members.append(group)
                    except ServiceError:
                        certain = False
            if certain and all(g.application is not None and g.application.phase == 'applied' for g in members):
                processed.add(source.source_ref)
    return frozenset(processed)
