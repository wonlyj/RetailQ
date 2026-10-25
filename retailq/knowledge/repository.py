"""Short transactions only. Runtime serializes all knowledge writers."""
from dataclasses import asdict

from sqlalchemy import select, update

from retailq.db import Database
from retailq.errors import ServiceError
from retailq.knowledge.contracts import ChunkDraft, ChunkRecord
from retailq.knowledge.models import KnowledgeChunk
from retailq.knowledge.text import content_fingerprint
from retailq.repositories import database_errors


def record(row: KnowledgeChunk) -> ChunkRecord:
    draft = ChunkDraft(row.category,row.questions,row.answer,row.section_path,row.content_type,bool(row.is_key_clause))
    return ChunkRecord(row.id,draft,row.prev_chunk_id,row.next_chunk_id,row.vector_id,row.vectorize_status)


class KnowledgeRepository:
    def __init__(self,database: Database):
        self.database = database

    @database_errors
    def import_chunks(self,drafts: list[ChunkDraft]) -> list[int]:
        if not drafts:
            return []
        with self.database.sessions.begin() as session:
            rows = session.scalars(select(KnowledgeChunk).order_by(KnowledgeChunk.id)).all()
            by_id = {r.id:record(r) for r in rows}
            for first in by_id.values():
                if first.prev_chunk_id is not None:
                    continue
                chain, seen, current = [],set(),first
                while current and current.id not in seen:
                    seen.add(current.id)
                    chain.append(current)
                    current = by_id.get(current.next_chunk_id)
                if [r.draft for r in chain] == drafts and current is None:
                    return [r.id for r in chain]
            # Matching complete payload but attached to another chain is unsafe.
            if any(r.draft in drafts for r in by_id.values()):
                raise ServiceError('knowledge_chain_conflict',409,'知识块已在不同顺序链中，请核查导入来源。')
            new = [KnowledgeChunk(**asdict(d),vectorize_status='pending') for d in drafts]
            session.add_all(new)
            session.flush()
            for i,row in enumerate(new):
                if not 1 <= row.id <= 2**63-1:
                    raise ValueError('knowledge id exceeds Milvus INT64 range')
                row.prev_chunk_id = new[i-1].id if i else None
                row.next_chunk_id = new[i+1].id if i+1<len(new) else None
            return [row.id for row in new]

    @database_errors
    def all_chunks(self) -> list[ChunkRecord]:
        with self.database.sessions() as session:
            return [record(r) for r in session.scalars(select(KnowledgeChunk).order_by(KnowledgeChunk.id))]

    @database_errors
    def pending(self,limit: int,after_id: int=0) -> list[ChunkRecord]:
        if not 1 <= limit <= 10000:
            raise ValueError('invalid pending batch size')
        with self.database.sessions() as session:
            rows=session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.vectorize_status=='pending',KnowledgeChunk.id>after_id).order_by(KnowledgeChunk.id).limit(limit))
            return [record(r) for r in rows]

    @database_errors
    def get_chunks(self,ids: list[int]) -> dict[int,ChunkRecord]:
        with self.database.sessions() as session:
            return {r.id:record(r) for r in session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.id.in_(ids)))}

    @database_errors
    def mark_done(self,chunk_id: int,vector_id: str,expected_fingerprint: str) -> bool:
        if vector_id != str(chunk_id):
            raise ValueError('vector primary key must match chunk id')
        with self.database.sessions.begin() as session:
            row=session.scalar(select(KnowledgeChunk).where(KnowledgeChunk.id==chunk_id).with_for_update())
            if row is None or content_fingerprint(record(row).draft) != expected_fingerprint:
                return False
            row.vector_id, row.vectorize_status = vector_id,'done'
            return True

    @database_errors
    def reset_pending(self,ids: list[int]) -> int:
        with self.database.sessions.begin() as session:
            result=session.execute(update(KnowledgeChunk).where(KnowledgeChunk.id.in_(ids)).values(vectorize_status='pending',vector_id=None))
            return result.rowcount
