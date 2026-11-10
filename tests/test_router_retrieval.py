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
            assert query=='退货条件' and len(items)==3
            return items
    result=PolicyRetriever(Engine(),Reranker()).retrieve('本单能退吗',('本单能退吗','退货条件','退款流程'),base.snapshot,'policy_multi',deadline=time.monotonic()+5)
    assert len(result.candidates)==3 and len(calls)==3
    assert all(row[0] is base.snapshot and row[2]=='policy' for row in calls)
    assert result.strategy=='policy_multi'
    assert result.understanding.raw_question=='本单能退吗'
    assert result.understanding.normalized_query=='退货条件'


@pytest.mark.parametrize('mode,queries,expected',[
    ('policy_single',('退货政策是什么',),'退货政策是什么'),
    ('policy_multi',('订单1002尺码不合适想换货','商品尺码不合适换货的条件','换货流程'),'商品尺码不合适换货的条件'),
    ('policy_multi',('订单1001签收8天没拆封能退吗','签收8天未拆封商品能否退货','超期退货例外'),'签收8天未拆封商品能否退货'),
])
def test_policy_reranks_once_with_policy_question_without_overwriting_original(mode,queries,expected):
    from retailq.knowledge.policy_retrieval import PolicyRetriever
    base=retrieval(1)
    candidates=tuple(replace(c,draft=replace(c.draft,content_type='policy')) for c in base.candidates)
    recalled=[];reranked=[]
    class Engine:
        def retrieve(self,understood,snapshot,strategy,filters,*,deadline):
            recalled.append(understood.normalized_query)
            return SimpleNamespace(snapshot=snapshot,candidates=candidates)
    class Reranker:
        def rerank(self,query,items,*,deadline):
            reranked.append(query)
            return items
    result=PolicyRetriever(Engine(),Reranker()).retrieve(queries[0],queries,base.snapshot,mode,deadline=time.monotonic()+5)
    assert recalled==list(queries) and reranked==[expected]
    assert result.understanding.raw_question==queries[0]
    assert result.understanding.normalized_query==expected


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
