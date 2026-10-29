import pytest

from retailq.knowledge.evaluation.store import EvaluationStore


def test_resume_rejects_identity_drift_and_duplicate_strategy(tmp_path):
    store = EvaluationStore.open_new(tmp_path/'run', {'corpus':'one'})
    store.checkpoint({'eval_id':'A01','strategy':'dense','phase':'complete'})
    with pytest.raises(ValueError):
        EvaluationStore.resume(tmp_path/'run', {'corpus':'two'})
    with pytest.raises(ValueError):
        store.checkpoint({'eval_id':'A01','strategy':'dense','phase':'complete'})
    assert len(EvaluationStore.resume(tmp_path/'run', {'corpus':'one'}).rows()) == 1


def test_sealed_evaluation_is_read_only(tmp_path):
    store=EvaluationStore.open_new(tmp_path/'run', {'corpus':'one'})
    store.seal('中断后终止')
    with pytest.raises(ValueError):
        store.checkpoint({'eval_id':'A01','strategy':'dense','phase':'complete'})


def test_staged_checkpoints_can_resume_but_cannot_regress(tmp_path):
    store=EvaluationStore.open_new(tmp_path/'run', {'corpus':'one'})
    store.checkpoint({'eval_id':'A01','strategy':'bm25','phase':'retrieval','hits':[4]})
    store.checkpoint({'eval_id':'A01','strategy':'bm25','phase':'generation','answer':'supported'})
    assert store.rows()[0]['hits']==[4]
    with pytest.raises(ValueError):
        store.checkpoint({'eval_id':'A01','strategy':'bm25','phase':'retrieval','hits':[5]})
    resumed=EvaluationStore.resume(tmp_path/'run', {'corpus':'one'})
    assert resumed.rows()[0]['answer']=='supported'


def test_store_requires_new_directory_and_does_not_overwrite(tmp_path):
    path=tmp_path/'run';path.mkdir();(path/'keep.txt').write_text('keep',encoding='utf-8')
    with pytest.raises(FileExistsError): EvaluationStore.open_new(path, {'corpus':'one'})
    assert (path/'keep.txt').read_text(encoding='utf-8')=='keep'


def test_failure_receipt_does_not_replace_partial_checkpoint(tmp_path):
    store=EvaluationStore.open_new(tmp_path/'run', {'corpus':'one'})
    store.checkpoint({'eval_id':'A01','strategy':'dense','phase':'retrieval','hits':[1]})
    store.record_error('A01','dense','generation','upstream_timeout')
    assert store.rows()[0]['phase']=='retrieval'
    assert store.errors()[0]['code']=='upstream_timeout'


def test_completion_requires_every_strategy_receipt(tmp_path):
    store=EvaluationStore.open_new(tmp_path/'run', {'expected':4})
    store.checkpoint({'eval_id':'A01','strategy':'dense','phase':'complete'})
    with pytest.raises(ValueError):
        store.complete()
    assert store.manifest()['status']=='running'


def test_finalizing_marker_can_recover_after_receipts_are_complete(tmp_path):
    store=EvaluationStore.open_new(tmp_path/'run', {'expected':4,'case_ids':['A01']})
    for strategy in ('dense','bm25','hybrid','hybrid_rerank'):
        store.checkpoint({'eval_id':'A01','strategy':strategy,'phase':'complete'})
    store.begin_finalization()
    assert store.manifest()['status']=='finalizing'
    with pytest.raises(ValueError): store.checkpoint({'eval_id':'A02','strategy':'dense','phase':'complete'})
    store.finish_finalization()
    assert store.manifest()['status']=='completed'


def test_seal_has_a_durable_recovery_marker(tmp_path):
    store=EvaluationStore.open_new(tmp_path/'run', {'expected':4})
    store.begin_sealing('operator stop')
    assert store.manifest()['status']=='sealing'
    assert store.manifest()['reason']=='operator stop'
    store.finish_sealing()
    assert store.manifest()['status']=='sealed'
