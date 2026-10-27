"""Read sources and apply a reviewed group in a single short SQL transaction."""
from dataclasses import asdict
from typing import Callable, Literal

from sqlalchemy import select

from retailq.errors import ServiceError
from retailq.knowledge.contracts import ChunkDraft, ConversationSnapshot, MiningMessage
from retailq.knowledge.models import KnowledgeChunk, QAStaging
from retailq.knowledge.repository import KnowledgeRepository, record
from retailq.knowledge.review_contracts import (
    MemberDisposition, Page, PreparedApplication, ReviewDecision, ReviewGroup, SourceSummary,
    normalize_category,
)
from retailq.knowledge.review_store import review_error
from retailq.knowledge.text import content_fingerprint
from retailq.models import Conversation, Message
from retailq.repositories import database_errors


def paginate(items, offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid pagination')
    return Page(items=tuple(items[offset:offset + limit]), total=len(items), offset=offset, limit=limit)


class ReviewRepository:
    def __init__(self, knowledge: KnowledgeRepository):
        self.knowledge = knowledge
        self.database = knowledge.database

    @staticmethod
    def _eligibility(conversation, messages):
        if conversation.status != '已结束':
            return False, '仅已结束会话可以选择。'
        if not messages or messages[0].role != 'user':
            return False, '缺少首条用户消息。'
        last = messages[-1]
        if last.role != 'assistant' or not last.content or not last.content.strip() or last.tool_calls:
            return False, '末条消息不是完整、非空的客服答复。'
        return True, ''

    @staticmethod
    def _snapshot(conversation, messages):
        maximum = messages[-1].id if messages else 0
        return ConversationSnapshot(f'conversation:{conversation.id}:message:{maximum}', conversation.id,
                                    maximum, tuple(MiningMessage(m.id, m.role, m.content) for m in messages))

    @database_errors
    def list_sources(self, *, query='', eligibility='all', offset=0, limit=20):
        if eligibility not in ('all', 'eligible', 'ineligible'):
            raise ValueError('invalid eligibility filter')
        with self.database.sessions() as session:
            sources = session.scalars(select(Conversation).order_by(Conversation.id)).all()
            # One message scan avoids one SQL query per displayed source.
            messages = {}
            for m in session.scalars(select(Message).order_by(Message.id)):
                messages.setdefault(m.conversation_id, []).append(m)
            items = []
            for source in sources:
                rows = messages.get(source.id, [])
                eligible, reason = self._eligibility(source, rows)
                if eligibility != 'all' and eligible != (eligibility == 'eligible'):
                    continue
                if query and query not in str(source.id) and query not in source.user_id and not any(
                        query in (m.content or '') for m in rows):
                    continue
                items.append(SourceSummary(id=source.id, status=source.status, eligibility=eligible,
                                            reason=reason, message_count=len(rows),
                                            max_message_id=rows[-1].id if rows else None))
        return paginate(items, offset, limit)

    @database_errors
    def source_detail(self, cid):
        with self.database.sessions() as session:
            source = session.get(Conversation, cid)
            if source is None:
                raise ServiceError('knowledge_source_not_found', 404, '来源会话不存在。')
            rows = session.scalars(select(Message).where(Message.conversation_id == cid).order_by(Message.id)).all()
            return self._snapshot(source, rows)

    @database_errors
    def freeze_sources(self, ids):
        if not ids or len(ids) > 100 or len(set(ids)) != len(ids) or any(
                type(cid) is not int or not 1 <= cid <= 2**63 - 1 for cid in ids):
            raise ValueError('select 1–100 unique valid conversation IDs')
        result = []
        with self.database.sessions() as session:
            for cid in sorted(ids):
                source = session.get(Conversation, cid)
                rows = session.scalars(select(Message).where(Message.conversation_id == cid).order_by(Message.id)).all()
                if source is None or not self._eligibility(source, rows)[0]:
                    raise ServiceError('knowledge_source_ineligible', 409, f'会话{cid}不存在或不符合抽取条件，请刷新来源。')
                result.append(self._snapshot(source, rows))
        return tuple(result)

    @database_errors
    def categories(self, *, query='', offset=0, limit=20):
        with self.database.sessions() as session:
            # DISTINCT under ai_ci would merge meaningful case/accent distinctions.
            values = session.scalars(select(KnowledgeChunk.category)).all()
        return paginate(sorted({s for s in values if s.strip() and query in s}), offset, limit)

    @staticmethod
    def _matching_members(group, rows):
        expected = {m.staging_id: (m.batch_no, m.source_ref, m.question, m.answer) for m in group.members}
        return {r.id: (r.batch_no, r.source_ref, r.question, r.answer) for r in rows} == expected

    @staticmethod
    def _dispositions(group, decision, chunk_id):
        result = []
        for member in group.members:
            status = 'discarded'
            if decision.kind == 'rejected':
                reason = 'human_rejected'
            elif group.target_chunk_id is not None:
                reason = 'merge_existing'
            elif member.staging_id == group.representative_id:
                status, reason = 'kept', 'new'
            else:
                reason = 'same_group'
            result.append(MemberDisposition(staging_id=member.staging_id, status=status, reason=reason, chunk_id=chunk_id))
        return tuple(result)

    @database_errors
    def apply_group(self, group: ReviewGroup, decision: ReviewDecision,
                    prepare: Callable[[PreparedApplication], None]) -> PreparedApplication:
        with self.database.sessions.begin() as session:
            rows = session.scalars(select(QAStaging).where(QAStaging.id.in_(
                [m.staging_id for m in group.members])).order_by(QAStaging.id).with_for_update()).all()
            if not self._matching_members(group, rows) or any(r.status != 'extracted' for r in rows):
                raise review_error(message='暂存问答已变化或已处理，请核查。')
            target, fingerprint = None, None
            if decision.kind == 'approved':
                if group.target_chunk_id is not None:
                    if decision.category is not None:
                        raise review_error(message='归并操作不能修改已有分类。')
                    target = session.scalar(select(KnowledgeChunk).where(
                        KnowledgeChunk.id == group.target_chunk_id).with_for_update())
                    if target is None or content_fingerprint(record(target).draft) != group.target_fingerprint:
                        raise review_error(message='归并目标不存在或内容已变化，请核查。')
                else:
                    draft = ChunkDraft(normalize_category(decision.category), '\n'.join(group.questions),
                                       group.answer, None, 'faq')
                    target = KnowledgeChunk(**asdict(draft), vectorize_status='pending')
                    session.add(target)
                    session.flush()
                    if not 1 <= target.id <= 2**63 - 1:
                        raise review_error(message='知识ID超过向量库支持范围。')
                fingerprint = content_fingerprint(record(target).draft)
            elif decision.category is not None:
                raise review_error(message='拒绝操作不能提交分类。')
            cid = target.id if target else None
            dispositions = self._dispositions(group, decision, cid)
            by_id = {m.staging_id: m.status for m in dispositions}
            for row in rows:
                row.status = by_id[row.id]
            prepared = PreparedApplication(operation_id=decision.operation_id, chunk_id=cid,
                                            fingerprint=fingerprint, members=dispositions)
            prepare(prepared)  # Any file error aborts the SQL transaction.
            return prepared

    @database_errors
    def inspect_application(self, group: ReviewGroup) -> Literal['committed', 'not_committed', 'conflict']:
        if not group.application or not group.decision:
            return 'conflict'
        prepared = group.application.prepared
        with self.database.sessions() as session:
            rows = session.scalars(select(QAStaging).where(QAStaging.id.in_(
                [m.staging_id for m in group.members]))).all()
            if not self._matching_members(group, rows):
                return 'conflict'
            if prepared.members != self._dispositions(group, group.decision, prepared.chunk_id):
                return 'conflict'
            # A previously flushed ID may now be occupied by unrelated data. All
            # members still extracted is not evidence of this transaction committing.
            if all(r.status == 'extracted' for r in rows):
                return 'not_committed'
            expected = {m.staging_id: m.status for m in prepared.members}
            if any(r.status != expected.get(r.id) for r in rows):
                return 'conflict'
            if group.decision.kind == 'rejected':
                return 'committed' if prepared.chunk_id is None and prepared.fingerprint is None else 'conflict'
            target = session.get(KnowledgeChunk, prepared.chunk_id)
            if target is None or content_fingerprint(record(target).draft) != prepared.fingerprint:
                return 'conflict'
            if group.target_chunk_id is not None and (target.id != group.target_chunk_id or
                                                       prepared.fingerprint != group.target_fingerprint):
                return 'conflict'
            return 'committed'
