"""Short transactions only. Runtime serializes all knowledge writers."""
from dataclasses import asdict

from sqlalchemy import select, update

from retailq.db import Database
from retailq.errors import ServiceError
from retailq.knowledge.contracts import ChunkDraft, ChunkRecord, QAProposal, StagedQA, MiningMessage, ConversationSnapshot, DedupGroup
from retailq.knowledge.models import KnowledgeChunk, QAStaging
from retailq.knowledge.text import content_fingerprint
from retailq.repositories import database_errors
from retailq.models import FAQ,Conversation,Message


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

    @database_errors
    def faq_drafts(self) -> list[ChunkDraft]:
        with self.database.sessions() as session:
            return [ChunkDraft(r.category,r.question,r.answer,f'legacy-faq/{r.id}','faq') for r in session.scalars(select(FAQ).order_by(FAQ.id))]

    @database_errors
    def closed_conversations(self) -> list[ConversationSnapshot]:
        result=[]
        with self.database.sessions() as session:
            ids=session.scalars(select(Conversation.id).where(Conversation.status=='已结束').order_by(Conversation.id)).all()
            for cid in ids:
                rows=session.scalars(select(Message).where(Message.conversation_id==cid).order_by(Message.id)).all()
                if not rows or rows[0].role!='user' or rows[-1].role!='assistant' or not rows[-1].content:continue
                messages=tuple(MiningMessage(r.id,r.role,r.content) for r in rows)
                result.append(ConversationSnapshot(f'conversation:{cid}:message:{rows[-1].id}',cid,rows[-1].id,messages))
        return result

    @staticmethod
    def _staged(row):
        return StagedQA(row.id,row.batch_no,row.source_ref,row.question,row.answer,row.status)

    @database_errors
    def stage(self,batch_no: str,proposals: list[QAProposal]) -> list[StagedQA]:
        if not 1<=len(batch_no)<=64:raise ValueError('invalid batch number')
        with self.database.sessions.begin() as session:
            existing=session.scalars(select(QAStaging).where(QAStaging.batch_no.collate('utf8mb4_0900_bin')==batch_no)).all()
            result=[]
            for proposal in proposals:
                if not proposal.source_ref or len(proposal.source_ref)>255:raise ValueError('invalid source reference')
                if any(not s.strip() or len(s.encode('utf-8'))>65535 for s in [proposal.question,proposal.answer]):raise ValueError('QA TEXT limit')
                match=next((r for r in existing if (r.source_ref,r.question,r.answer)==(proposal.source_ref,proposal.question,proposal.answer)),None)
                if match is None:
                    match=QAStaging(batch_no=batch_no,source_ref=proposal.source_ref,question=proposal.question,answer=proposal.answer,status='extracted')
                    session.add(match);session.flush();existing.append(match)
                result.append(self._staged(match))
            return result

    @database_errors
    def staging(self,batch_nos: list[str]) -> list[StagedQA]:
        with self.database.sessions() as session:
            rows=session.scalars(select(QAStaging).where(QAStaging.batch_no.collate('utf8mb4_0900_bin').in_(batch_nos)).order_by(QAStaging.id))
            return [self._staged(row) for row in rows]

    @database_errors
    def apply_dedup(self,groups: list[DedupGroup]) -> list[int]:
        result=[]
        for group in groups:
            with self.database.sessions.begin() as session:
                rows=session.scalars(select(QAStaging).where(QAStaging.id.in_(group.member_ids)).order_by(QAStaging.id).with_for_update()).all()
                if {r.id for r in rows}!=set(group.member_ids) or set(group.questions)!={r.question for r in rows}:raise ValueError('invalid dedup group membership')
                if group.target_chunk_id is not None:
                    target=session.get(KnowledgeChunk,group.target_chunk_id)
                    if target is None:raise ValueError('missing existing knowledge target')
                    for row in rows:row.status='discarded'
                else:
                    representative=next((r for r in rows if r.id==group.representative_id),None)
                    if representative is None:raise ValueError('missing group representative')
                    draft=ChunkDraft('对话知识','\n'.join(group.questions),representative.answer,None,'faq',False)
                    existing=session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.category=='对话知识')).all()
                    target=next((r for r in existing if record(r).draft==draft),None)
                    unresolved=[r for r in rows if r.status=='extracted']
                    if target is None:
                        if len(unresolved)!=len(rows):raise ValueError('resolved staging has no matching knowledge')
                        target=KnowledgeChunk(**asdict(draft),vectorize_status='pending')
                        session.add(target);session.flush()
                        if not 1<=target.id<=2**63-1:raise ValueError('knowledge id exceeds INT64')
                    for row in unresolved:row.status='kept' if row.id==representative.id else 'discarded'
                result.append(target.id)
        return result
