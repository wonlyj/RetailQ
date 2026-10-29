import pytest

from retailq.knowledge.faith_repository import FaithEvaluationCase, FaithRepository
from retailq.knowledge.faith_store import FaithStore
from retailq.coordination import Coordinator, RuntimeIdentity


@pytest.mark.mysql
def test_faith_case_counts_runs_and_reopens_resolved(test_database, tmp_path):
    coordinator = Coordinator(tmp_path/'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', tmp_path/'knowledge'))
    store = FaithStore(tmp_path/'faith', coordinator)
    repository = FaithRepository(test_database, store)
    case = FaithEvaluationCase('A01', 'A_policy', '问题', 'hybrid_rerank', '无依据答案', '缺少证据', (), 'judge')
    store.begin('run1', {'version':'1'})
    first = repository.record(case, eval_run_id='run1')
    repeated = repository.record(case, eval_run_id='run1')
    assert first.id == repeated.id and repeated.seen_count == 1
    store.complete('run1')
    resolved = repository.resolve('A01', '已解决', '已补充规则')
    assert resolved.resolved_at is not None
    store.begin('run2', {'version':'1'})
    second = repository.record(case, eval_run_id='run2')
    assert second.seen_count == 2 and second.status == '未解决' and second.resolution is None
    assert second.resolved_at == resolved.resolved_at
    store.complete('run2')


@pytest.mark.mysql
@pytest.mark.parametrize('old_citations', [None, []])
def test_old_citation_formats_and_commit_recovery(test_database, tmp_path, old_citations):
    from sqlalchemy import select
    from retailq.knowledge.rag_models import FaithCase
    coordinator = Coordinator(tmp_path/'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', tmp_path/'knowledge'))
    store = FaithStore(tmp_path/'faith', coordinator)
    repository = FaithRepository(test_database, store)
    with test_database.sessions.begin() as session:
        session.add(FaithCase(eval_id='A01', bucket='A_policy', query='问题', strategy='hybrid_rerank',
                              answer='旧回答', reason='旧理由', citations=old_citations, judge_model='judge'))
    case = FaithEvaluationCase('A01', 'A_policy', '问题', 'hybrid_rerank', '新回答', '缺少证据', (), 'judge')
    store.begin('run1', {'version':'1'})
    assert repository.record(case, eval_run_id='run1').seen_count == 2
    store.close()
    resumed = FaithStore(tmp_path/'faith', coordinator)
    repository = FaithRepository(test_database, resumed)
    resumed.resume('run1')
    assert repository.record(case, eval_run_id='run1').seen_count == 2
    resumed.complete('run1')
    with test_database.sessions() as session:
        row = session.scalar(select(FaithCase).where(FaithCase.eval_id == 'A01'))
        assert row.citations == {'eval_run_id':'run1', 'evidence':[]}


@pytest.mark.mysql
def test_incomplete_sql_receipt_prevents_completion(test_database, tmp_path):
    from retailq.errors import ServiceError
    coordinator = Coordinator(tmp_path/'locks', RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', tmp_path/'knowledge'))
    store = FaithStore(tmp_path/'faith', coordinator)
    FaithRepository(test_database, store)
    store.begin('run1', {'version':'1'})
    store.register('run1', 'A01')
    with pytest.raises(ServiceError): store.complete('run1')
    store.seal('run1', '确认中断')
    with pytest.raises(ServiceError): store.resume('run1')
