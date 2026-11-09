from dataclasses import replace
from types import SimpleNamespace
import time
import pytest
from retailq.errors import ServiceError
from tests.test_rag_answer import retrieval


def test_policy_queries_share_snapshot_deduplicate_and_rerank_once():
    from retailq.knowledge.policy_retrieval import PolicyRetriever
    base=retrieval(3)
    candidates=tuple(replace(c,draft=replace(c.draft,content_type='policy')) for c in base.candidates)
    calls=[]
    class Engine:
        def retrieve(self,understood,snapshot,strategy,filters,*,deadline):
            calls.append((snapshot,understood.normalized_query,filters.content_type))
            return SimpleNamespace(**{**vars(base),'snapshot':snapshot,'candidates':candidates})
    class Reranker:
        def rerank(self,query,items,*,deadline):
            assert query=='本单能退吗' and len(items)==3
            return items
    result=PolicyRetriever(Engine(),Reranker()).retrieve('本单能退吗',('本单能退吗','退货条件','退款流程'),base.snapshot,'policy_multi',deadline=time.monotonic()+5)
    assert len(result.candidates)==3 and len(calls)==3
    assert all(row[0] is base.snapshot and row[2]=='policy' for row in calls)
    assert result.strategy=='policy_multi'


def test_expired_group_starts_no_search():
    from retailq.knowledge.policy_retrieval import PolicyRetriever
    with pytest.raises(ServiceError):
        PolicyRetriever(None,None).retrieve('问题',('问题',),None,'policy_single',deadline=time.monotonic()-1)


def test_partial_failure_does_not_return_half_evidence():
    from retailq.knowledge.policy_retrieval import PolicyRetriever
    base=retrieval(1)
    class Engine:
        n=0
        def retrieve(self,*args,**kwargs):
            self.n+=1
            if self.n==2:raise ServiceError('failure',503,'test')
            return base
    with pytest.raises(ServiceError):
        PolicyRetriever(Engine(),None).retrieve('问题',('问题','条件','流程'),base.snapshot,'policy_multi',deadline=time.monotonic()+5)
