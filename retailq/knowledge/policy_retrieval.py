"""Bounded query-side policy recall; index content remains single-copy."""
from dataclasses import replace
import time
from retailq.errors import ServiceError
from retailq.knowledge.retrieval_contracts import QueryUnderstanding,RetrievalFilters,RetrievalResult


class PolicyRetriever:
    def __init__(self,engine,reranker):self.engine,self.reranker=engine,reranker

    def retrieve(self,question,queries,snapshot,mode,*,deadline):
        def check():
            if time.monotonic()>=deadline:raise ServiceError('knowledge_query_timeout',504,'本轮政策检索已超时。')
        check()
        expected=1 if mode=='policy_single' else None
        if (mode not in ('policy_single','policy_multi') or not queries or queries[0]!=question
                or len(queries)!=len(set(queries)) or (expected and len(queries)!=expected)
                or (mode=='policy_multi' and not 3<=len(queries)<=5)):
            raise ServiceError('policy_queries_invalid',422,'政策查询组不完整。')
        versions={}
        for source in snapshot.sources:
            for chunk_id in source.chunk_ids:
                version=(source.key,source.digest)
                if chunk_id in versions and versions[chunk_id]!=version:
                    raise ServiceError('knowledge_identity_mismatch',409,'政策来源版本冲突。')
                versions[chunk_id]=version
        pool={};scores={}
        for query in queries:
            check()
            understood=QueryUnderstanding(question,query,query)
            result=self.engine.retrieve(understood,snapshot,'hybrid',RetrievalFilters(content_type='policy'),deadline=deadline)
            if result.snapshot is not snapshot:raise ServiceError('knowledge_identity_mismatch',409,'政策查询快照不一致。')
            check()
            for rank,candidate in enumerate(result.candidates[:10],1):
                if candidate.draft.content_type!='policy':continue
                key=(candidate.id,versions.get(candidate.id,('review','')))
                if key in pool and pool[key].draft!=candidate.draft:
                    raise ServiceError('knowledge_identity_mismatch',409,'同一政策版本内容冲突。')
                pool[key]=candidate;scores[key]=scores.get(key,0)+1/(60+rank)
        ordered=sorted(pool,key=lambda key:(-scores[key],key[0]))
        merged=tuple(replace(pool[key],rank=i,score=scores[key]) for i,key in enumerate(ordered,1))
        check()
        # Binding an order is separate from finding the governing policy. The
        # first expansion is the canonical policy question, retaining material
        # conditions without order identifiers or selection instructions.
        policy_question=queries[1] if mode=='policy_multi' else question
        final=self.reranker.rerank(policy_question,merged,deadline=deadline) if merged else ()
        check()
        return RetrievalResult(QueryUnderstanding(question,policy_question,policy_question),snapshot,mode,tuple(final[:10]),())
