import json
from pathlib import Path
import pytest

from scripts.evaluate_rag import safe_output, report_command
from retailq.knowledge.evaluation.store import EvaluationStore


def test_output_must_stay_in_eval_artifacts(tmp_path):
    with pytest.raises(ValueError): safe_output(tmp_path/'outside')
    with pytest.raises(ValueError): safe_output(Path('artifacts/evals/ch04/..'))


def test_report_keeps_incomplete_status_and_missing_denominators(tmp_path):
    store=EvaluationStore.open_new(tmp_path/'run',{'split':'test','expected':320})
    store.record_error('A01','dense','retrieval','knowledge_query_timeout')
    report=report_command(store)
    assert report['complete'] is False and report['expected']==320
    assert report['overall']['dense']['faithfulness_macro'] is None
    assert report['failure_count']==1 and report['failures_by_phase']=={'retrieval':1}
    assert json.loads((tmp_path/'run'/'report.json').read_text(encoding='utf-8'))['complete'] is False


def test_report_is_not_complete_before_faith_receipt_finalization(tmp_path):
    store=EvaluationStore.open_new(tmp_path/'run',{'split':'test','expected':4,'case_ids':['A01']})
    for strategy in ('dense','bm25','hybrid','hybrid_rerank'):
        store.checkpoint({'eval_id':'A01','bucket':'A_policy','difficulty':'easy','strategy':strategy,
                          'phase':'complete','status':'refused','metrics':{},'claims':[]})
    report=report_command(store)
    assert report['rows_complete'] is True and report['complete'] is False


def test_identity_contains_no_credentials_and_changes_when_code_changes(tmp_path):
    from types import SimpleNamespace
    from scripts.evaluate_rag import build_identity
    from retailq.config import Settings
    source=tmp_path/'sample.py';source.write_text('a',encoding='utf-8')
    settings=Settings(_env_file=None,openai_model='judge',openai_api_key='secret-token',embedding_api_key='embed-secret')
    snapshot=SimpleNamespace(corpus_hash='abc',collection='test_collection')
    first=build_identity(settings,snapshot,'calibration',files=[source])
    assert 'secret-token' not in repr(first) and 'embed-secret' not in repr(first)
    source.write_text('b',encoding='utf-8')
    second=build_identity(settings,snapshot,'calibration',files=[source])
    assert first != second and first['split']=='calibration' and first['expected']==80


def test_seal_completes_faith_receipt_before_marking_report_readonly(tmp_path):
    from scripts.evaluate_rag import seal_run
    calls=[]
    class Faith:
        def resume(self, run_id): calls.append(('resume',run_id))
        def seal(self, run_id, reason): calls.append(('seal',run_id,reason))
        def close(self): calls.append(('close',))
    store=EvaluationStore.open_new(tmp_path/'run',{'split':'test','expected':320})
    seal_run(store,Faith(),'run','manual stop')
    assert calls==[('resume','run'),('seal','run','manual stop'),('close',)]
    assert store.manifest()['status']=='sealed'
    assert json.loads((tmp_path/'run'/'report.json').read_text(encoding='utf-8'))['status']=='sealed'


def test_seal_recovers_when_faith_terminal_written_before_manifest(tmp_path):
    from scripts.evaluate_rag import seal_run
    from retailq.coordination import Coordinator,RuntimeIdentity
    from retailq.knowledge.faith_store import FaithStore
    identity={'split':'test','expected':320}
    coordinator=Coordinator(tmp_path/'locks',RuntimeIdentity('127.0.0.1',3307,'retailq_test','http://127.0.0.1:19531',tmp_path/'knowledge'))
    store=EvaluationStore.open_new(tmp_path/'run',identity)
    faith=FaithStore(tmp_path/'faith',coordinator)
    faith.begin('run',identity)
    store.begin_sealing('manual stop')
    faith.seal('run','manual stop')
    seal_run(store,FaithStore(tmp_path/'faith',coordinator),'run','manual stop')
    assert store.manifest()['status']=='sealed'
    next_run=FaithStore(tmp_path/'faith',coordinator)
    next_run.begin('next',identity)
    next_run.seal('next','done')
