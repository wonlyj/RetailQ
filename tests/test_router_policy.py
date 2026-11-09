from dataclasses import replace
from types import SimpleNamespace
import json
import pytest
from retailq.config import Settings
from retailq.errors import ServiceError


def test_policy_mode_identity_and_loaded_bytes(tmp_path):
    from retailq.knowledge.workflow_policy import workflow_policy_identity,freeze_workflow_policy
    snapshot=SimpleNamespace(corpus_hash='fixed')
    path=tmp_path/'policy.json'
    settings=Settings(_env_file=None,workflow_policy_single_confidence_path=str(path))
    identity=workflow_policy_identity(settings,snapshot,'policy_single')
    path.write_text(json.dumps({'identity':identity,'calibrated':True,'thresholds':{'policy_single':.7}}),encoding='utf-8')
    policy,sha=freeze_workflow_policy(settings,snapshot,'policy_single')
    assert policy.identity==identity and len(sha)==64
    assert workflow_policy_identity(settings,snapshot,'policy_multi')!=identity
    with pytest.raises(ServiceError):freeze_workflow_policy(settings,SimpleNamespace(corpus_hash='changed'),'policy_single')


def test_calibration_uses_only_bound_calibration_labels():
    from scripts.calibrate_router import fit_policy,report_digest
    mode='policy_multi'
    rows=[{'id':str(i),'mode':mode,'score':.9 if i<6 else .1,'candidate_id':i} for i in range(12)]
    report={'split':'calibration','complete':True,'rows':rows,'identities':{mode:{'strategy':mode}}}
    annotations={'report_sha':report_digest(report),'labels':[
        {'id':str(i),'mode':mode,'candidate_id':i,'relevant':i<6,'reason':'controlled relevance label'} for i in range(12)]}
    policy=fit_policy(report,annotations,mode)
    assert policy.thresholds[mode]==.9
    with pytest.raises(ValueError):fit_policy({**report,'split':'test'},annotations,mode)
    with pytest.raises(ValueError):fit_policy(report,{**annotations,'report_sha':'stale'},mode)
