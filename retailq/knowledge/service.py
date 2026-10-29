"""MySQL is authoritative. Vector failures leave pending rows for the next run."""
from __future__ import annotations
import threading
import time
from typing import Callable,TYPE_CHECKING

from retailq.errors import ServiceError
from retailq.knowledge.contracts import EmbeddingProvider,SourceDocument,VectorItem,VectorizeReport,VectorFailure
from retailq.knowledge.markdown import chunk_document,structures
from retailq.knowledge.repository import KnowledgeRepository
from retailq.knowledge.text import content_fingerprint,embedding_text,normalize_text
from retailq.knowledge.token_budget import BGETokenBudget
if TYPE_CHECKING:
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
    def __init__(self,repository: KnowledgeRepository,embedding: EmbeddingProvider,vectors: MilvusKnowledgeStore,budget: BGETokenBudget,*,top_k: int=3,min_score: float=0.0,batch_size: int=16,query_timeout_seconds: float=5.0,chunk_target_chars: int=600,chunk_overlap_chars: int=100,retrieval_engine=None,normalizer=None,corpus=None,index_builder=None):
        self.repository,self.embedding,self.vectors,self.budget=repository,embedding,vectors,budget
        self.top_k,self.min_score,self.batch_size=top_k,min_score,batch_size
        self.query_timeout_seconds=query_timeout_seconds
        self.chunk_target_chars,self.chunk_overlap_chars=chunk_target_chars,chunk_overlap_chars
        self.retrieval_engine,self.normalizer,self.corpus,self.index_builder=retrieval_engine,normalizer,corpus,index_builder

    def retrieve(self, question, *, strategy='hybrid_rerank', filters=None, deadline=None):
        if self.retrieval_engine is None or self.normalizer is None or self.corpus is None:
            raise ServiceError('knowledge_retrieval_unavailable', 503, '新知识检索尚未配置。')
        end=deadline if deadline is not None else time.monotonic()+self.query_timeout_seconds
        snapshot=self.corpus.active()
        understood=self.normalizer.understand(question,deadline=end)
        return self.retrieval_engine.retrieve(understood,snapshot,strategy,filters,deadline=end)

    def import_documents(self,documents: list[SourceDocument]) -> list[int]:
        result=[]
        for document in documents:
            result.extend(self.repository.import_chunks(chunk_document(document,self.budget,target_chars=self.chunk_target_chars,overlap_chars=self.chunk_overlap_chars)))
        return result

    def import_faq(self) -> list[int]:
        if self.corpus is not None:
            raise ServiceError('knowledge_corpus_invalid',409,'活动语料使用版本化导入，请先核查来源。')
        result=[]
        for draft in self.repository.faq_drafts():
            self.budget.validate(embedding_text(draft))
            result.extend(self.repository.import_chunks([draft]))
        return result

    def status(self) -> dict[str,int]:
        rows=self.repository.all_chunks()
        result={'chunks':len(rows),'pending':sum(r.vectorize_status=='pending' for r in rows),'done':sum(r.vectorize_status=='done' for r in rows)}
        if self.corpus is not None:
            from retailq.knowledge.corpus import is_active
            snapshot=self.corpus.active()
            result.update(active=sum(is_active(row,snapshot) for row in rows),
                          history=sum(not is_active(row,snapshot) for row in rows),
                          active_pending=sum(is_active(row,snapshot) and row.vectorize_status != 'done' for row in rows))
        return result

    def vectorize_pending(self,*,stop: threading.Event | None=None,fault: Callable[[str],None] | None=None,only_ids: tuple[int,...] | None=None) -> VectorizeReport:
        snapshot=None
        if self.corpus is not None:
            from retailq.knowledge.corpus import is_active
            snapshot=self.corpus.active()
            rows=self.repository.get_chunks(list(snapshot.active_ids))
            eligible={item_id for item_id,row in rows.items() if is_active(row,snapshot)}
            if only_ids is not None and not set(only_ids) <= eligible:
                raise ServiceError('knowledge_corpus_invalid',409,'知识块不属于当前活动语料。')
            if self.vectors.collection != snapshot.collection:
                raise ServiceError('knowledge_identity_mismatch',409,'检索集合与活动语料不一致。')
            only_ids=tuple(sorted(eligible if only_ids is None else only_ids))
        def pending_count():
            if only_ids is None:
                return self.status()['pending']
            return sum(r.vectorize_status=='pending' for r in self.repository.get_chunks(list(only_ids)).values())
        before=pending_count()
        done=failed=after_id=0
        failures=[]
        while stop is None or not stop.is_set():
            rows=self.repository.pending(self.batch_size,after_id,only_ids=only_ids)
            if not rows:break
            after_id=rows[-1].id
            if fault:fault('after_mysql_pending')
            try:
                vectors=self.embedding.embed([embedding_text(r.draft) for r in rows])
                if len(vectors)!=len(rows):raise ValueError('wrong vector batch count')
                if snapshot is None:
                    items=[VectorItem(r.id,v,content_fingerprint(r.draft)) for r,v in zip(rows,vectors)]
                else:
                    from retailq.knowledge.retrieval_contracts import IndexedChunk
                    items=[]
                    for r,v in zip(rows,vectors):
                        fingerprint=content_fingerprint(r.draft)
                        source=next((s for s in snapshot.sources if r.id in s.chunk_ids),None)
                        items.append(IndexedChunk(r.id,v,fingerprint,embedding_text(r.draft),r.draft.category,
                            r.draft.content_type or 'faq',source.key if source else 'review',source.digest if source else fingerprint))
                ids=self.vectors.upsert(items)
                if set(ids)!={r.id for r in rows}:raise ValueError('wrong vector ids')
                if snapshot is not None and self.vectors.fingerprints(ids)!={item.id:item.fingerprint for item in items}:
                    raise ValueError('vector fingerprint mismatch')
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
        return VectorizeReport(before,done,failed,pending_count(),tuple(failures))

    def reconcile(self) -> list[int]:
        rows=[r for r in self.repository.all_chunks() if r.vectorize_status=='done']
        if self.corpus is not None:
            from retailq.knowledge.corpus import is_active
            snapshot=self.corpus.active()
            if self.vectors.collection != snapshot.collection:
                raise ServiceError('knowledge_identity_mismatch',409,'检索集合与活动语料不一致。')
            rows=[row for row in rows if is_active(row,snapshot)]
        missing=[]
        for offset in range(0,len(rows),128):
            batch=rows[offset:offset+128]
            actual=self.vectors.fingerprints([r.id for r in batch])
            missing.extend(r.id for r in batch if r.vector_id!=str(r.id) or actual.get(r.id)!=content_fingerprint(r.draft))
        self.repository.reset_pending(missing)
        return missing

    def search_faq(self,keyword: str) -> list[dict]:
        if self.retrieval_engine is not None:
            result=self.retrieve(keyword)
            return [{'id':item.id,'question':item.draft.questions,'answer':complete_excerpt(item.draft.answer),
                     'category':item.draft.category} for item in result.candidates]
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
