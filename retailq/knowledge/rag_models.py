from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text, text
from sqlalchemy.dialects.mysql import BIGINT, ENUM, INTEGER
from sqlalchemy.orm import Mapped, mapped_column

from retailq.models import Base


class LowConfidenceQuestion(Base):
    __tablename__ = 'low_confidence_questions'
    __table_args__ = (Index('idx_source', 'source'), Index('idx_created_at', 'created_at'))
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    conversation_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey('conversations.id', name='fk_lcq_conversation'))
    raw_question: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(ENUM('retrieval_low_conf', 'self_check', 'user_feedback'))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text('CURRENT_TIMESTAMP'))


class FaithCase(Base):
    __tablename__ = 'faith_cases'
    __table_args__ = (Index('uk_eval_id', 'eval_id', unique=True), Index('idx_status', 'status'), Index('idx_last_seen_at', 'last_seen_at'))
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    eval_id: Mapped[str] = mapped_column(String(16))
    bucket: Mapped[str] = mapped_column(String(24))
    query: Mapped[str] = mapped_column(String(512))
    strategy: Mapped[str] = mapped_column(String(24), server_default='hybrid_rerank')
    answer: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    citations: Mapped[dict | list | None] = mapped_column(JSON)
    judge_model: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(ENUM('未解决', '已解决', '无需解决'), server_default='未解决')
    seen_count: Mapped[int] = mapped_column(INTEGER(unsigned=True), server_default=text('1'))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=text('CURRENT_TIMESTAMP'))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=text('CURRENT_TIMESTAMP'))
    resolution: Mapped[str | None] = mapped_column(String(300))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
