from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.mysql import BIGINT, ENUM, TINYINT
from sqlalchemy.orm import Mapped, mapped_column

from retailq.models import Base


class KnowledgeChunk(Base):
    __tablename__ = 'knowledge_chunks'
    __table_args__ = (Index('idx_category','category'),Index('idx_vectorize_status','vectorize_status'))
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True),primary_key=True,autoincrement=True)
    category: Mapped[str] = mapped_column(String(255))
    questions: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    section_path: Mapped[str | None] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(32))
    is_key_clause: Mapped[int] = mapped_column(TINYINT(display_width=1),server_default=text('0'))
    prev_chunk_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True),ForeignKey('knowledge_chunks.id',name='fk_chunks_prev',ondelete='SET NULL'))
    next_chunk_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True),ForeignKey('knowledge_chunks.id',name='fk_chunks_next',ondelete='SET NULL'))
    vector_id: Mapped[str | None] = mapped_column(String(64))
    vectorize_status: Mapped[str] = mapped_column(ENUM('pending','done'),server_default='pending')
    created_at: Mapped[datetime] = mapped_column(DateTime,server_default=text('CURRENT_TIMESTAMP'))
    updated_at: Mapped[datetime] = mapped_column(DateTime,server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))


class QAStaging(Base):
    __tablename__ = 'qa_extraction_staging'
    __table_args__ = (Index('idx_batch_no','batch_no'),Index('idx_status','status'))
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True),primary_key=True,autoincrement=True)
    batch_no: Mapped[str] = mapped_column(String(64))
    source_ref: Mapped[str | None] = mapped_column(String(255))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(ENUM('extracted','kept','discarded'),server_default='extracted')
    created_at: Mapped[datetime] = mapped_column(DateTime,server_default=text('CURRENT_TIMESTAMP'))
