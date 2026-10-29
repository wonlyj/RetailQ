"""Idempotent cross-run faithfulness cases with SQL receipts."""
from dataclasses import dataclass
import json
import re

from sqlalchemy import func, select

from retailq.errors import ServiceError
from retailq.knowledge.rag_models import FaithCase
from retailq.repositories import database_errors


@dataclass(frozen=True)
class FaithEvaluationCase:
    eval_id: str
    bucket: str
    query: str
    strategy: str
    answer: str
    reason: str
    evidence: tuple
    judge_model: str


class FaithRepository:
    def __init__(self, database, store):
        self.database, self.store = database, store
        store.verify = self.verify_receipts

    @database_errors
    def verify_receipts(self, run_id, eval_ids):
        with self.database.sessions() as session:
            rows = session.scalars(select(FaithCase).where(FaithCase.eval_id.in_(eval_ids))).all()
            return len(rows) == len(eval_ids) and all(isinstance(row.citations, dict) and row.citations.get('eval_run_id') == run_id for row in rows)

    @database_errors
    def record(self, case: FaithEvaluationCase, *, eval_run_id: str) -> FaithCase:
        if (not re.fullmatch(r'[A-Z0-9_-]{1,16}', case.eval_id) or case.strategy != 'hybrid_rerank'
                or not 1 <= len(case.bucket) <= 24 or not 1 <= len(case.query) <= 512
                or not case.answer.strip() or not case.reason.strip() or not 1 <= len(case.judge_model) <= 64):
            raise ValueError('invalid faithfulness case')
        self.store.register(eval_run_id, case.eval_id)
        envelope = {'eval_run_id':eval_run_id, 'evidence':json.loads(json.dumps(list(case.evidence), ensure_ascii=False, allow_nan=False))}
        with self.database.sessions.begin() as session:
            row = session.scalar(select(FaithCase).where(FaithCase.eval_id == case.eval_id).with_for_update())
            if row is None:
                row = FaithCase(eval_id=case.eval_id, bucket=case.bucket, query=case.query, strategy=case.strategy,
                                answer=case.answer, reason=case.reason, citations=envelope, judge_model=case.judge_model)
                session.add(row)
                session.flush()
                session.refresh(row)
                return row
            if isinstance(row.citations, dict) and row.citations.get('eval_run_id') == eval_run_id:
                if (row.bucket, row.query, row.strategy, row.answer, row.reason, row.citations, row.judge_model) != (
                        case.bucket, case.query, case.strategy, case.answer, case.reason, envelope, case.judge_model):
                    raise ServiceError('faith_receipt_conflict', 409, '同一轮评估回执与已提交结果不一致。')
                return row
            row.bucket, row.query, row.strategy = case.bucket, case.query, case.strategy
            row.answer, row.reason, row.citations, row.judge_model = case.answer, case.reason, envelope, case.judge_model
            row.status, row.resolution = '未解决', None
            row.seen_count += 1
            row.last_seen_at = func.now()
            session.flush(); session.refresh(row)
            return row

    @database_errors
    def resolve(self, eval_id, status, resolution):
        if status not in ('已解决','无需解决','未解决') or (status != '未解决' and (not isinstance(resolution, str) or not resolution.strip() or len(resolution) > 300)):
            raise ValueError('invalid resolution')
        with self.database.sessions.begin() as session:
            row = session.scalar(select(FaithCase).where(FaithCase.eval_id == eval_id).with_for_update())
            if row is None: raise ServiceError('faith_case_not_found', 404, '未找到评估个案。')
            row.status = status
            row.resolution = resolution.strip() if status != '未解决' else None
            if status != '未解决': row.resolved_at = func.now()
            session.flush(); session.refresh(row)
            return row

    @database_errors
    def list_cases(self, status=None, offset=0, limit=20):
        if status not in (None, '未解决', '已解决', '无需解决') or offset < 0 or not 1 <= limit <= 100:
            raise ValueError('invalid pagination')
        with self.database.sessions() as session:
            statement = select(FaithCase)
            if status is not None: statement = statement.where(FaithCase.status == status)
            return list(session.scalars(statement.order_by(FaithCase.last_seen_at.desc(), FaithCase.id.desc()).offset(offset).limit(limit)))
