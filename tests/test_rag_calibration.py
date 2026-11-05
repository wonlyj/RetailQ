import math
import pytest

from scripts.calibrate_rag import fit_confidence_policy


def bind_checkpoint(report, annotations):
    import hashlib, json
    annotations['checkpoint_sha256']=hashlib.sha256(json.dumps(report,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


def relevance_fixture():
    import hashlib, json
    identity={'policy_identity':{'model':'m','corpus_hash':'abc','prompt_hash':'p','query_hash':'q','strategy':'all-v2'},'run':'frozen',
              'case_ids':['missing','answerable','unrelated']}
    rows=[]; labels=[]
    for strategy in ('dense','bm25','hybrid','hybrid_rerank'):
        for name,score,answerable,relevant in [('missing',.8,False,True),('answerable',.2,True,True),('unrelated',.1,True,False)]:
            rows.append({'eval_id':name,'strategy':strategy,'phase':'complete','answerable':answerable,
                         'candidates':[{'id':len(rows)+1,'score':score}]})
            labels.append({'eval_id':name,'strategy':strategy,'candidate_id':rows[-1]['candidates'][0]['id'],
                           'relevant':relevant,'reason':'人工核对候选内容'})
    report={'complete':True,'split':'calibration','identity':identity,'rows':rows}
    digest=hashlib.sha256(json.dumps(report,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()
    return report, {'identity':identity,'labels':labels,'checkpoint_sha256':digest}


def test_relevance_fit_uses_candidate_relevance_not_answerability():
    from scripts.calibrate_rag import fit_relevance_policy
    report,labels=relevance_fixture()
    policy=fit_relevance_policy(report,labels)
    assert policy.thresholds==dict.fromkeys(('dense','bm25','hybrid','hybrid_rerank'),.2)


def test_calibration_cli_requires_explicit_relevance_annotations_or_legacy_mode():
    from scripts.calibrate_rag import main
    with pytest.raises(SystemExit): main(['--report','missing-report.json'])


@pytest.mark.parametrize('mutation', ['missing','duplicate','candidate','reason','label','identity','nonfinite'])
def test_relevance_fit_rejects_unreliable_annotations(mutation):
    from scripts.calibrate_rag import fit_relevance_policy
    report,annotations=relevance_fixture(); labels=annotations['labels']
    if mutation=='missing': labels.pop()
    if mutation=='duplicate': labels.append(labels[0].copy())
    if mutation=='candidate': labels[0]['candidate_id']=999
    if mutation=='reason': labels[0]['reason']='  '
    if mutation=='label': labels[0]['relevant']=1
    if mutation=='identity': annotations['identity']={'run':'other'}
    if mutation=='nonfinite':
        report['rows'][0]['candidates'][0]['score']=float('nan')
        bind_checkpoint(report,annotations)
    with pytest.raises(ValueError): fit_relevance_policy(report,annotations)


def test_relevance_fit_handles_empty_candidates_and_rejects_wrong_strategy_inventory():
    from scripts.calibrate_rag import fit_relevance_policy
    report,annotations=relevance_fixture()
    for row,label in zip(report['rows'],annotations['labels']):
        if row['eval_id']=='unrelated':
            row['candidates']=[];label['candidate_id']=None
    bind_checkpoint(report,annotations)
    assert fit_relevance_policy(report,annotations).thresholds['dense']==pytest.approx(.2)
    annotations['labels'][2]['relevant']=True
    with pytest.raises(ValueError): fit_relevance_policy(report,annotations)


def test_relevance_fit_rejects_single_class_and_test_split():
    from scripts.calibrate_rag import fit_relevance_policy
    report,annotations=relevance_fixture()
    for label in annotations['labels']: label['relevant']=True
    with pytest.raises(ValueError): fit_relevance_policy(report,annotations)


def test_relevance_fit_rejects_score_changed_after_annotation():
    from scripts.calibrate_rag import fit_relevance_policy
    report,annotations=relevance_fixture()
    report['rows'][0]['candidates'][0]['score']=.001
    with pytest.raises(ValueError): fit_relevance_policy(report,annotations)


def test_relevance_fit_rejects_omitting_same_question_from_every_strategy():
    from scripts.calibrate_rag import fit_relevance_policy
    report,annotations=relevance_fixture()
    report['rows']=[r for r in report['rows'] if r['eval_id']!='unrelated']
    annotations['labels']=[r for r in annotations['labels'] if r['eval_id']!='unrelated']
    for label in annotations['labels']:
        if label['eval_id']=='answerable': label['relevant']=False
    bind_checkpoint(report,annotations)
    with pytest.raises(ValueError): fit_relevance_policy(report,annotations)
    report,annotations=relevance_fixture();report['split']='test'
    with pytest.raises(ValueError): fit_relevance_policy(report,annotations)


def test_evaluation_policy_and_hash_are_frozen_from_same_bytes(tmp_path):
    import json
    from hashlib import sha256
    from types import SimpleNamespace
    from retailq.config import Settings
    from retailq.knowledge.policy import freeze_evaluation_policy, policy_identity
    path=tmp_path/'policy.json';settings=Settings(_env_file=None,rag_confidence_path=str(path))
    snapshot=SimpleNamespace(corpus_hash='abc')
    data={'identity':policy_identity(settings,snapshot,'dense'),'calibrated':True,
          'thresholds':{'dense':0.3,'bm25':3.0,'hybrid':0.05,'hybrid_rerank':0.7}}
    raw=json.dumps(data).encode();path.write_bytes(raw)
    policy,digest=freeze_evaluation_policy(settings,snapshot)
    data['thresholds']['dense']=0.9;path.write_text(json.dumps(data),encoding='utf-8')
    result=SimpleNamespace(strategy='dense',snapshot=snapshot,candidates=[SimpleNamespace(score=0.5)])
    assert digest==sha256(raw).hexdigest() and policy.allows(result)


def test_policy_identity_changes_when_normalization_code_changes(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from retailq.config import Settings
    from retailq.knowledge.policy import policy_identity
    settings=Settings(_env_file=None);snapshot=SimpleNamespace(corpus_hash='abc')
    original=Path.read_bytes
    before=policy_identity(settings,snapshot,'dense')
    def changed(path):
        content=original(path)
        return content+b'\n# changed' if path.name=='query.py' else content
    monkeypatch.setattr(Path,'read_bytes',changed)
    assert policy_identity(settings,snapshot,'dense')!=before


@pytest.mark.parametrize('changed_file',['retrieval.py','milvus_store.py','embedding.py','text.py'])
def test_relevance_policy_rejects_retrieval_code_drift_without_breaking_legacy(tmp_path, monkeypatch, changed_file):
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from retailq.config import Settings
    from retailq.errors import ServiceError
    from retailq.knowledge.policy import DynamicConfidencePolicy, policy_identity
    settings=Settings(_env_file=None,rag_confidence_path=str(tmp_path/'policy.json'))
    snapshot=SimpleNamespace(corpus_hash='abc')
    result=SimpleNamespace(strategy='hybrid_rerank',snapshot=snapshot,candidates=[SimpleNamespace(score=.5)])
    identity=policy_identity(settings,snapshot,'hybrid_rerank',version=2)
    (tmp_path/'policy.json').write_text(json.dumps({'identity':identity,'calibrated':True,'thresholds':{'hybrid_rerank':.4}}),encoding='utf-8')
    assert DynamicConfidencePolicy(settings).allows(result)
    original=Path.read_bytes
    def changed(path):
        data=original(path)
        return data+b'\n# drift' if path.name==changed_file else data
    monkeypatch.setattr(Path,'read_bytes',changed)
    with pytest.raises(ServiceError): DynamicConfidencePolicy(settings).allows(result)


def calibration_rows():
    rows=[]
    for strategy in ('dense','bm25','hybrid','hybrid_rerank'):
        for b in 'ABCDE':
            for i in range(1,5):
                positive=b!='D' and i>2
                score=([.1,.2,.3,.9][i-1])*(1 if strategy=='dense' else 10)
                rows.append({'eval_id':f'{b}{i:02}','bucket':{'A':'A_policy','B':'B_model','C':'C_colloquial','D':'D_missing','E':'E_multi'}[b],
                             'strategy':strategy,'phase':'complete','status':'refused','answerable':b!='D',
                             'evidence_coverage':1.0 if positive else 0.0,'candidates':[{'score':score}],
                             'faithfulness':None,'claims':[]})
    return rows


def test_fit_policy_does_not_share_score_scales():
    report={'complete':True,'split':'calibration','identity':{'policy_identity':{'model':'m','corpus_hash':'abc','prompt_hash':'p','query_hash':'q','strategy':'all'}},'rows':calibration_rows()}
    policy=fit_confidence_policy(report)
    assert set(policy.thresholds)=={'dense','bm25','hybrid','hybrid_rerank'}
    assert policy.thresholds['dense']==pytest.approx(.3)
    assert policy.thresholds['bm25']==pytest.approx(3.0)


def test_fit_rejects_incomplete_test_and_unjudged_answer():
    identity={'policy_identity':{'model':'m','corpus_hash':'abc','prompt_hash':'p','query_hash':'q','strategy':'all'}}
    rows=calibration_rows()
    for report in ({'complete':False,'split':'calibration','identity':identity,'rows':rows},
                   {'complete':True,'split':'test','identity':identity,'rows':rows},
                   {'complete':True,'split':'calibration','identity':identity,'rows':rows[:-1]}):
        with pytest.raises(ValueError): fit_confidence_policy(report)
    rows[0]['status']='answered';rows[0]['faithfulness']=None
    with pytest.raises(ValueError): fit_confidence_policy({'complete':True,'split':'calibration','identity':identity,'rows':rows})


def test_missing_case_never_becomes_positive_from_vacuous_coverage():
    rows=calibration_rows()
    for row in rows:
        if row['bucket']=='D_missing': row['evidence_coverage']=1.0
    report={'complete':True,'split':'calibration','identity':{'policy_identity':{'model':'m','corpus_hash':'abc','prompt_hash':'p','query_hash':'q','strategy':'all'}},'rows':rows}
    policy=fit_confidence_policy(report)
    assert policy.thresholds['dense']==pytest.approx(.3)


def test_policy_publication_preserves_existing_file(tmp_path):
    import json
    from retailq.knowledge.answer import ConfidencePolicy
    from scripts.calibrate_rag import publish_policy
    target=tmp_path/'policy.json';target.write_text('{"old":true}',encoding='utf-8')
    policy=ConfidencePolicy({'model':'m','corpus_hash':'abc','prompt_hash':'p','query_hash':'q','strategy':'all'},
                            {'dense':.3,'bm25':3.0,'hybrid':.05,'hybrid_rerank':.7})
    backup=publish_policy(policy,target)
    assert json.loads(backup.read_text(encoding='utf-8'))=={'old':True}
    assert json.loads(target.read_text(encoding='utf-8'))['calibrated'] is True


def test_fit_rejects_nonfinite_scores_and_degenerate_classes():
    identity={'policy_identity':{'model':'m','corpus_hash':'abc','prompt_hash':'p','query_hash':'q','strategy':'all'}}
    rows=calibration_rows();rows[0]['candidates'][0]['score']=float('nan')
    with pytest.raises(ValueError): fit_confidence_policy({'complete':True,'split':'calibration','identity':identity,'rows':rows})


def test_fit_rejects_different_question_sets_across_strategies():
    identity={'policy_identity':{'model':'m','corpus_hash':'abc','prompt_hash':'p','query_hash':'q','strategy':'all'}}
    rows=calibration_rows()
    next(r for r in rows if r['strategy']=='bm25' and r['eval_id']=='A01')['eval_id']='A99'
    with pytest.raises(ValueError):
        fit_confidence_policy({'complete':True,'split':'calibration','identity':identity,'rows':rows})


def test_fit_rejects_malformed_or_inconsistent_coverage():
    identity={'policy_identity':{'model':'m','corpus_hash':'abc','prompt_hash':'p','query_hash':'q','strategy':'all'}}
    rows=calibration_rows();rows[0]['evidence_coverage']=1.5
    with pytest.raises(ValueError):
        fit_confidence_policy({'complete':True,'split':'calibration','identity':identity,'rows':rows})
    rows=calibration_rows()
    for row in rows: row['answerable']=False;row['evidence_coverage']=None
    with pytest.raises(ValueError): fit_confidence_policy({'complete':True,'split':'calibration','identity':identity,'rows':rows})


def test_policy_allows_each_strategy_only_under_same_frozen_identity(tmp_path):
    import json
    from types import SimpleNamespace
    from retailq.config import Settings
    from retailq.knowledge.answer import ConfidencePolicy
    from retailq.knowledge.policy import DynamicConfidencePolicy, policy_identity
    settings=Settings(_env_file=None,rag_confidence_path=str(tmp_path/'policy.json'),openai_model='model')
    snapshot=SimpleNamespace(corpus_hash='abc')
    identity=policy_identity(settings,snapshot,'hybrid_rerank')
    assert identity==policy_identity(settings,snapshot,'dense')
    thresholds={'dense':.3,'bm25':3.0,'hybrid':.05,'hybrid_rerank':.7}
    (tmp_path/'policy.json').write_text(json.dumps({'identity':identity,'calibrated':True,'thresholds':thresholds}),encoding='utf-8')
    policy=DynamicConfidencePolicy(settings)
    assert policy.allows(SimpleNamespace(strategy='dense',snapshot=snapshot,candidates=[SimpleNamespace(score=.4)]))
    assert not policy.allows(SimpleNamespace(strategy='bm25',snapshot=snapshot,candidates=[SimpleNamespace(score=2)]))
    changed=settings.model_copy(update={'max_output_tokens':settings.max_output_tokens+1})
    assert policy_identity(changed,snapshot,'dense') != identity


@pytest.mark.anyio
async def test_prompt_samples_check_self_assessment_and_judge_independently(tmp_path):
    from types import SimpleNamespace
    from scripts.calibrate_rag import validate_prompt_samples
    from retailq.knowledge.evaluation.judge import JudgeResult
    cases=[{'id':'P01','query':'问题','evidence':[],'candidate_answer':'错误[1]',
            'expected_useful':True,'expected_claim_supported':False,'reason':'错误'}]
    class Gateway:
        async def answer(self,bundle): return SimpleNamespace(useful=True,answer='正确[1]',reason='',citations=[1])
    class Judge:
        async def judge(self,answer,citations): return JudgeResult(({'text':'错误','supported':False,'citations':[],'reason':'缺证'},),0.0)
    bundle=SimpleNamespace(citations=(SimpleNamespace(number=1),))
    async def understand(query): return query
    report=await validate_prompt_samples(cases,understand,Gateway(),Judge(),lambda case,query:bundle,tmp_path/'checks.json')
    assert report['passed']==1 and report['failed']==0
    assert report['results'][0]['self_check_pass'] and report['results'][0]['judge_pass']
    cases[0]['query']='被修改的问题'
    with pytest.raises(ValueError):
        await validate_prompt_samples(cases,understand,Gateway(),Judge(),lambda case,query:bundle,tmp_path/'checks.json')
