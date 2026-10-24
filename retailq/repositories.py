"""Database operations are synchronous and transactional; no live Session escapes."""
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
import json
import secrets

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from retailq.db import Database
from retailq.errors import ServiceError
from retailq.models import Conversation, FAQ, Message, Ticket
from retailq.schemas import HistoryMessage


def database_errors(method):
    @wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except SQLAlchemyError:
            raise ServiceError('database_unavailable', 503, '数据库操作未确认成功，请稍后重试。') from None
    return guarded


@dataclass(frozen=True)
class MessageRecord:
    id: int
    conversation_id: int
    role: str
    content: str | None
    tool_calls: list[dict] | None
    tool_call_id: str | None
    created_at: datetime


class ChatRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _owned(session, cid: int, user_id: str, *, lock: bool = False):
        # Opaque identities must match Python/tool-call semantics even though
        # the supplied schema intentionally keeps its default ai_ci collation.
        statement = select(Conversation).where(Conversation.id == cid,
            Conversation.user_id.collate('utf8mb4_0900_bin') == user_id)
        conversation = session.scalar(statement.with_for_update() if lock else statement)
        if conversation is None:
            raise ServiceError('conversation_not_found', 404, '未找到当前用户的会话，请新建会话。')
        return conversation

    @database_errors
    def start_turn(self, user_id: str, conversation_id: int | None,
                   history: list[HistoryMessage], message: str) -> int:
        if conversation_id is not None and history:
            raise ServiceError('ambiguous_history', 422, '已有会话不能同时导入客户端历史。')
        with self.database.sessions.begin() as session:
            if conversation_id is None:
                conversation = Conversation(user_id=user_id)
                session.add(conversation)
                session.flush()
                conversation_id = conversation.id
                for item in history:
                    session.add(Message(conversation_id=conversation_id, role=item.role, content=item.content))
            else:
                self._owned(session, conversation_id, user_id, lock=True)
            session.add(Message(conversation_id=conversation_id, role='user', content=message))
        return conversation_id

    @database_errors
    def load_messages(self, conversation_id: int, user_id: str) -> list[MessageRecord]:
        with self.database.sessions() as session:
            self._owned(session, conversation_id, user_id)
            rows = session.scalars(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)).all()
            return [MessageRecord(r.id, r.conversation_id, r.role, r.content, r.tool_calls, r.tool_call_id, r.created_at) for r in rows]

    @database_errors
    def append_message(self, conversation_id: int, user_id: str, role: str, content: str | None,
                       tool_calls: list[dict] | None = None, tool_call_id: str | None = None) -> int:
        with self.database.sessions.begin() as session:
            self._owned(session, conversation_id, user_id, lock=True)
            if role == 'tool':
                existing = session.scalar(select(Message).where(Message.conversation_id == conversation_id,
                    Message.role == 'tool', Message.tool_call_id.collate('utf8mb4_0900_bin') == tool_call_id))
                if existing is not None:
                    return existing.id
            row = Message(conversation_id=conversation_id, role=role, content=content,
                          tool_calls=tool_calls, tool_call_id=tool_call_id)
            session.add(row)
            session.flush()
            return row.id

    @database_errors
    def search_faq(self, keyword: str) -> list[dict]:
        with self.database.sessions() as session:
            rows = session.scalars(select(FAQ).where(or_(
                FAQ.question.contains(keyword, autoescape=True), FAQ.answer.contains(keyword, autoescape=True),
                FAQ.category.contains(keyword, autoescape=True),
            )).order_by(FAQ.id).limit(3)).all()
            return [{'id': r.id, 'question': r.question, 'answer': r.answer[:1200], 'category': r.category} for r in rows]

    @database_errors
    def create_ticket(self, conversation_id: int, user_id: str, description: str,
                      ticket_type: str, tool_call_id: str) -> dict:
        with self.database.sessions.begin() as session:
            conversation = self._owned(session, conversation_id, user_id, lock=True)
            existing = session.scalar(select(Message).where(Message.conversation_id == conversation_id,
                Message.role == 'tool', Message.tool_call_id.collate('utf8mb4_0900_bin') == tool_call_id))
            if existing is not None:
                payload = json.loads(existing.content)
                if payload.get('ok'):
                    return payload['data']
                raise ServiceError('tool_call_already_resolved', 409, '该工具申请已经处理，请勿重复提交。')
            requests = session.scalars(select(Message).where(Message.conversation_id == conversation_id,
                Message.role == 'assistant', Message.tool_calls.is_not(None))).all()
            if not any(c.get('id') == tool_call_id and c.get('function', {}).get('name') == 'create_ticket'
                       for r in requests for c in (r.tool_calls or [])):
                raise ServiceError('missing_tool_request', 409, '缺少对应的工单创建申请。')
            ticket_no = 'T' + datetime.now().strftime('%Y%m%d') + secrets.token_hex(8).upper()
            session.add(Ticket(ticket_no=ticket_no, conversation_id=conversation_id,
                               description=description, ticket_type=ticket_type))
            conversation.status = '已转人工'
            result = {'ticket_no': ticket_no, 'ticket_type': ticket_type, 'status': '待处理'}
            session.add(Message(conversation_id=conversation_id, role='tool', tool_call_id=tool_call_id,
                                content=json.dumps({'ok': True, 'data': result}, ensure_ascii=False)))
        return result
