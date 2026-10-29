"""Atomic finalization of a persisted FAQ tool turn and optional refusal."""
import json
from sqlalchemy import select

from retailq.errors import ServiceError
from retailq.knowledge.rag_models import LowConfidenceQuestion
from retailq.models import Message
from retailq.repositories import ChatRepository, database_errors


class RAGRepository:
    def __init__(self, database):
        self.database = database

    def _add_pool(self, session, conversation_id, raw_question, source, reason):
        row = LowConfidenceQuestion(conversation_id=conversation_id, raw_question=raw_question,
                                    source=source, reason=reason)
        session.add(row)
        session.flush()
        return row

    @database_errors
    def finalize(self, conversation_id, user_id, tool_call_id, *, answer, source=None, reason=None):
        if not isinstance(answer, str) or not answer.strip() or source not in (None, 'retrieval_low_conf', 'self_check'):
            raise ValueError('invalid RAG finalization')
        if source and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError('refusal requires reason')
        with self.database.sessions.begin() as session:
            ChatRepository._owned(session, conversation_id, user_id, lock=True)
            tool = session.scalar(select(Message).where(Message.conversation_id == conversation_id, Message.role == 'tool',
                Message.tool_call_id.collate('utf8mb4_0900_bin') == tool_call_id).with_for_update())
            if tool is None:
                raise ServiceError('rag_receipt_missing', 404, '本轮知识查询记录不存在。')
            try:
                receipt = json.loads(tool.content)
                if not isinstance(receipt, dict): raise ValueError()
            except (ValueError, TypeError):
                raise ServiceError('rag_receipt_invalid', 409, '知识查询记录损坏，请核查。') from None
            call = session.scalar(select(Message).where(Message.conversation_id == conversation_id,
                Message.role == 'assistant', Message.id < tool.id, Message.tool_calls.is_not(None)).order_by(Message.id.desc()).limit(1))
            if call is None or not any(c.get('id') == tool_call_id and c.get('function', {}).get('name') == 'query_faq'
                                       for c in call.tool_calls or []):
                raise ServiceError('rag_receipt_invalid', 409, '知识查询记录与工具申请不一致。')
            user = session.scalar(select(Message).where(Message.conversation_id == conversation_id,
                Message.role == 'user', Message.id < call.id).order_by(Message.id.desc()).limit(1))
            if user is None or not user.content:
                raise ServiceError('rag_receipt_invalid', 409, '无法确认用户原话。')
            prior = receipt.get('rag_finalization')
            if prior is not None:
                try:
                    existing = session.get(Message, prior['assistant_message_id'])
                    pool = session.get(LowConfidenceQuestion, prior['low_confidence_question_id']) if prior['low_confidence_question_id'] else None
                    if (existing is None or existing.conversation_id != conversation_id or existing.role != 'assistant' or existing.content != answer or
                            (source is None and pool is not None) or (source is not None and
                            (pool is None or pool.conversation_id != conversation_id or pool.raw_question != user.content or
                             pool.source != source or pool.reason != reason))):
                        raise ValueError()
                    return existing.id
                except (KeyError, TypeError, ValueError):
                    raise ServiceError('rag_receipt_invalid', 409, '知识回答回执与数据库不一致。') from None
            assistant = Message(conversation_id=conversation_id, role='assistant', content=answer)
            session.add(assistant); session.flush()
            pool = self._add_pool(session, conversation_id, user.content, source, reason) if source else None
            receipt['rag_finalization'] = {'assistant_message_id': assistant.id, 'low_confidence_question_id': pool.id if pool else None}
            tool.content = json.dumps(receipt, ensure_ascii=False, separators=(',', ':'))
            return assistant.id
