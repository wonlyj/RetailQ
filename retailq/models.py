"""Mappings of the supplied ch02 DDL. Initialization is owned by db/init SQL."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text, text
from sqlalchemy.dialects.mysql import BIGINT, ENUM
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(ENUM("进行中", "已转人工", "已结束"), server_default="进行中")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"))


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(ENUM("user", "assistant", "tool"))
    content: Mapped[str | None] = mapped_column(Text)
    tool_calls: Mapped[list | None] = mapped_column(JSON)
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class FAQ(Base):
    __tablename__ = "faq"
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(String(512))
    answer: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"))


class Ticket(Base):
    __tablename__ = "tickets"
    ticket_no: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), ForeignKey("conversations.id"), index=True)
    description: Mapped[str] = mapped_column(Text)
    ticket_type: Mapped[str] = mapped_column(ENUM("售后", "投诉", "咨询"))
    status: Mapped[str] = mapped_column(ENUM("待处理", "已处理"), server_default="待处理")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
