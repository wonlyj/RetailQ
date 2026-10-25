"""MySQL is authoritative. Vector failures leave pending rows for the next run."""
import threading
import time
from typing import Callable

from retailq.errors import ServiceError
from retailq.knowledge.contracts import EmbeddingProvider,SourceDocument,VectorItem,VectorizeReport,VectorFailure
from retailq.knowledge.markdown import chunk_document,structures
from retailq.knowledge.repository import KnowledgeRepository
from retailq.knowledge.text import content_fingerprint,embedding_text,normalize_text
from retailq.knowledge.token_budget import BGETokenBudget
from retailq.knowledge.vectors import MilvusKnowledgeStore


def complete_excerpt(answer: str,maximum: int=1200) -> str:
    if len(answer)<=maximum:return answer
    output=''
    for kind,value in structures(answer):
        if kind=='unit':
            if len(output+value)>maximum:break
            output+=value
        else:
            header,rows=value
            table=header
            included=0
            for row in rows:
                if len(output+table+row)>maximum:break
                table+=row;included+=1
            if included:output+=table
            if included<len(rows):break
    return normalize_text(output)


class KnowledgeService:
    def __init__(self,repository: KnowledgeRepository,embedding: EmbeddingProvider,vectors: MilvusKnowledgeStore,budget: BGETokenBudget,*,top_k: int=3,min_score: float=0.0,batch_size: int=16,query_timeout_seconds: float=5.0,chunk_target_chars: int=600,chunk_overlap_chars: int=100):
        self.repository,self.embedding,self.vectors,self.budget=repository,embedding,vectors,budget
        self.top_k,self.min_score,self.batch_size=top_k,min_score,batch_size
        self.query_timeout_seconds=query_timeout_seconds
        self.chunk_target_chars,self.chunk_overlap_chars=chunk_target_chars,chunk_overlap_chars

    def import_documents(self,documents: list[SourceDocument]) -> list[int]:
        result=[]
        for document in documents:
            result.extend(self.repository.import_chunks(chunk_document(document,self.budget,target_chars=self.chunk_target_chars,overlap_chars=self.chunk_overlap_chars)))
        return result

    def import_faq(self) -> list[int]:
        result=[]
        for draft in self.repository.faq_drafts():
            self.budget.validate(embedding_text(draft))
            result.extend(self.repository.import_chunks([draft]))
        return result

    def status(self) -> dict[str,int]:
        rows=self.repository.all_chunks()
        return {'chunks':len(rows),'pending':sum(r.vectorize_status=='pending' for r in rows),'done':sum(r.vectorize_status=='done' for r in rows)}

    def vectorize_pending(self,*,stop: threading.Event | None=None,fault: Callable[[str],None] | None=None) -> VectorizeReport:
        before=self.status()['pending']
        done=failed=after_id=0
        failures=[]
        while stop is None or not stop.is_set():
            rows=self.repository.pending(self.batch_size,after_id)
            if not rows:break
            after_id=rows[-1].id
            if fault:fault('after_mysql_pending')
            try:
                vectors=self.embedding.embed([embedding_text(r.draft) for r in rows])
                if len(vectors)!=len(rows):raise ValueError('wrong vector batch count')
                items=[VectorItem(r.id,v,content_fingerprint(r.draft)) for r,v in zip(rows,vectors)]
                ids=self.vectors.upsert(items)
                if set(ids)!={r.id for r in rows}:raise ValueError('wrong vector ids')
            except Exception as exc:
                failed+=len(rows)
                code=exc.code if isinstance(exc,ServiceError) else 'knowledge_vectorize_failed'
                failures.extend(VectorFailure(row.id,code) for row in rows)
                continue
            # Kept outside the failure handler so crash hooks really interrupt.
            if fault:fault('after_milvus_upsert')
            for item in items:
                try:
                    confirmed=self.repository.mark_done(item.id,str(item.id),item.fingerprint)
                except Exception:
                    confirmed=False
                done+=int(confirmed);failed+=int(not confirmed)
                if not confirmed:failures.append(VectorFailure(item.id,'knowledge_done_not_confirmed'))
        return VectorizeReport(before,done,failed,self.status()['pending'],tuple(failures))

    def reconcile(self) -> list[int]:
        rows=[r for r in self.repository.all_chunks() if r.vectorize_status=='done']
        missing=[]
        for offset in range(0,len(rows),128):
            batch=rows[offset:offset+128]
            actual=self.vectors.fingerprints([r.id for r in batch])
            missing.extend(r.id for r in batch if r.vector_id!=str(r.id) or actual.get(r.id)!=content_fingerprint(r.draft))
        self.repository.reset_pending(missing)
        return missing

    def search_faq(self,keyword: str) -> list[dict]:
        deadline=time.monotonic()+self.query_timeout_seconds
        vector=self.embedding.embed([normalize_text(keyword)],deadline=deadline)[0]
        hits=self.vectors.search(vector,min(self.top_k*4,100),timeout_seconds=deadline-time.monotonic())
        rows=self.repository.get_chunks([hit.id for hit in hits])
        result=[]
        for hit in hits:
            row=rows.get(hit.id)
            if hit.score<self.min_score or row is None or row.vectorize_status!='done' or row.vector_id!=str(row.id):continue
            if hit.fingerprint!=content_fingerprint(row.draft):continue
            answer=complete_excerpt(row.draft.answer)
            if not answer:continue
            result.append({'id':row.id,'question':row.draft.questions,'answer':answer,'category':row.draft.category})
            if len(result)==self.top_k:break
        if time.monotonic()>=deadline:
            raise ServiceError('knowledge_query_timeout',504,'知识查询等待超时，请稍后重试。')
        return result
