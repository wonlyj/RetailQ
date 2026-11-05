from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, text
from sqlalchemy.dialects.mysql import BIGINT, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column
from retailq.models import Base


class WorkflowTurn(Base):
    __tablename__ = 'workflow_turns'
    turn_id: Mapped[str] = mapped_column(VARCHAR(32,charset='ascii',collation='ascii_bin'),primary_key=True)
    conversation_id: Mapped[int] = mapped_column(BIGINT(unsigned=True),ForeignKey('conversations.id'),index=True)
    user_message_id: Mapped[int] = mapped_column(BIGINT(unsigned=True),ForeignKey('messages.id'),unique=True)
    final_message_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True),ForeignKey('messages.id'),unique=True)
    status: Mapped[str] = mapped_column(String(16),server_default='active')
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime,server_default=text('CURRENT_TIMESTAMP'))
    updated_at: Mapped[datetime] = mapped_column(DateTime,server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))


class WorkflowAction(Base):
    __tablename__ = 'workflow_actions'
    __table_args__ = (Index('uq_workflow_turn_kind','turn_id','kind',unique=True),)
    action_id: Mapped[str] = mapped_column(VARCHAR(32,charset='ascii',collation='ascii_bin'),primary_key=True)
    turn_id: Mapped[str] = mapped_column(VARCHAR(32,charset='ascii',collation='ascii_bin'),ForeignKey('workflow_turns.turn_id'))
    origin_assistant_message_id: Mapped[int] = mapped_column(BIGINT(unsigned=True),ForeignKey('messages.id'))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16),server_default='offered')
    request_hash: Mapped[str | None] = mapped_column(VARCHAR(64,charset='ascii',collation='ascii_bin'))
    tool_call_id: Mapped[str] = mapped_column(VARCHAR(64,charset='ascii',collation='ascii_bin'),unique=True)
    request_message_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True),ForeignKey('messages.id'))
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime,server_default=text('CURRENT_TIMESTAMP'))
    updated_at: Mapped[datetime] = mapped_column(DateTime,server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
