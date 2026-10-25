import pytest
from sqlalchemy import select,update

from retailq.errors import ServiceError
from retailq.knowledge.contracts import ChunkDraft,VectorItem
from retailq.knowledge.models import KnowledgeChunk
from tests.support.knowledge import service,FakeEmbedding

pytestmark=[pytest.mark.mysql,pytest.mark.lite]


def test_pending_is_committed_before_embedding(service):
    service.repository.import_chunks([ChunkDraft('知识','问','完整答案。','new','manual')])
    original=service.embedding.embed
    def observed(texts,**kwargs):
        assert service.repository.database.engine.pool.checkedout()==0
        assert service.status()['pending']>0
        return original(texts,**kwargs)
    service.embedding.embed=observed
    assert service.vectorize_pending().done==1


def test_crash_after_upsert_reuses_ids(service):
    ids=service.repository.import_chunks([ChunkDraft('知识','问','完整答案。','new','manual')])
    def crash(phase):
        if phase=='after_milvus_upsert':raise RuntimeError('crash')
    with pytest.raises(RuntimeError):service.vectorize_pending(fault=crash)
    assert service.repository.pending(10)[0].id==ids[0]
    assert service.vectors.fingerprints(ids)
    before=len(service.repository.all_chunks())
    report=service.vectorize_pending()
    assert report.done==1 and report.pending_after==0
    assert len(service.repository.all_chunks())==before
    assert service.repository.get_chunks(ids)[ids[0]].vector_id==str(ids[0])


def test_failed_rows_remain_pending_while_next_rows_progress(service):
    ids=service.repository.import_chunks([ChunkDraft('知识','问','FAIL。','a','manual'),ChunkDraft('知识','问','完整答案。','b','manual')])
    original=service.embedding.embed
    def failed(texts,**kwargs):
        if 'FAIL' in texts[0]:raise ServiceError('embedding_unavailable',503,'failed')
        return original(texts,**kwargs)
    service.embedding.embed=failed;service.batch_size=1
    result=service.vectorize_pending()
    assert result.done==1 and result.failed==1 and result.pending_after==1
    assert service.repository.pending(10)[0].id==ids[0]


def test_done_missing_vector_reconcile(service):
    target=service.repository.all_chunks()[0].id
    service.vectors.client.delete('knowledge',ids=[target])
    assert service.reconcile()==[target]
    assert service.repository.pending(10)[0].id==target
    assert service.vectorize_pending().pending_after==0


def test_search_preserves_score_order_and_uses_mysql_answer(service):
    rows=service.repository.all_chunks()
    actual=service.search_faq('比较')
    assert [i['id'] for i in actual]==[rows[1].id,rows[0].id]
    assert actual[0]['answer']==rows[1].draft.answer


def test_orphan_pending_stale_vectors_are_never_returned(service):
    shipping=service.repository.all_chunks()[1]
    service.vectors.upsert([VectorItem(999999,FakeEmbedding().embed(['运费'])[0],'a'*64)])
    service.repository.reset_pending([shipping.id])
    assert service.search_faq('配送查询')==[]
    service.vectorize_pending()
    with service.repository.database.sessions.begin() as session:
        session.execute(update(KnowledgeChunk).where(KnowledgeChunk.id==shipping.id).values(answer='运费规则已修改。'))
    assert service.search_faq('配送查询')==[]


def test_faq_contract_has_no_internal_fields(service):
    items=service.search_faq('配送查询')
    assert items and set(items[0])=={'id','question','answer','category'}
    assert len(items[0]['answer'])<=1200


def test_long_answer_is_cut_only_at_complete_boundaries(service):
    service.repository.import_chunks([ChunkDraft('售后','退货','完整演示条款。'*200,'long','policy')])
    service.vectorize_pending()
    answers=service.search_faq('退货')
    assert all(len(row['answer'])<=1200 and row['answer'].endswith('。') for row in answers)


def test_dependency_failure_propagates_instead_of_miss(service):
    def failed(*a,**k):raise ServiceError('embedding_unavailable',503,'failed')
    service.embedding.embed=failed
    with pytest.raises(ServiceError):service.search_faq('配送查询')


def test_vectorization_failures_report_ids_and_safe_codes(service):
    ids=service.repository.import_chunks([ChunkDraft('知识','问','失败答案。','failure','manual')])
    def failed(*a,**k):raise ServiceError('embedding_unavailable',503,'private-response')
    service.embedding.embed=failed
    report=service.vectorize_pending()
    assert report.failures[0].chunk_id==ids[0]
    assert report.failures[0].code=='embedding_unavailable'
    assert 'private-response' not in repr(report)
