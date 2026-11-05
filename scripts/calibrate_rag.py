"""Fit separate retrieval score thresholds using only the calibration split."""
from collections import Counter
from hashlib import sha256
import argparse
import json
import math
from pathlib import Path
import uuid

from retailq.knowledge.answer import ConfidencePolicy
from retailq.knowledge.evaluation.runner import STRATEGIES
from retailq.knowledge.evaluation.runner import _plain
from retailq.knowledge.corpus import _write


def fit_relevance_policy(calibration, annotations):
    """Fit relevance gates from reviewed first-candidate labels, not answerability."""
    if (not isinstance(calibration, dict) or calibration.get('complete') is not True or
            calibration.get('split') != 'calibration' or not isinstance(annotations, dict) or
            annotations.get('identity') != calibration.get('identity')):
        raise ValueError('completed calibration and matching annotation identity required')
    checkpoint_hash=sha256(json.dumps(calibration,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()
    if annotations.get('checkpoint_sha256') != checkpoint_hash:
        raise ValueError('calibration checkpoint differs from annotated data')
    rows, labels = calibration.get('rows'), annotations.get('labels')
    identity = calibration.get('identity', {}).get('policy_identity')
    if (not isinstance(rows, list) or not rows or not isinstance(labels, list) or
            not isinstance(identity, dict) or set(identity) != {'model','corpus_hash','prompt_hash','query_hash','strategy'} or
            identity.get('strategy') != 'all-v2'):
        raise ValueError('invalid calibration data')
    indexed = {}
    for label in labels:
        if not isinstance(label, dict) or set(label) != {'eval_id','strategy','candidate_id','relevant','reason'}:
            raise ValueError('invalid relevance annotation')
        key = (label['eval_id'], label['strategy'])
        if (not all(isinstance(v, str) and v for v in key) or key in indexed or
                type(label['relevant']) is not bool or not isinstance(label['reason'], str) or not label['reason'].strip()):
            raise ValueError('invalid or duplicate relevance annotation')
        indexed[key] = label
    samples = {strategy: [] for strategy in STRATEGIES}
    seen = set()
    for row in rows:
        if not isinstance(row, dict): raise ValueError('invalid calibration row')
        key = (row.get('eval_id'), row.get('strategy'))
        if (not all(isinstance(v, str) and v for v in key) or key in seen or
                key not in indexed or key[1] not in samples or row.get('phase') != 'complete'):
            raise ValueError('invalid or unmatched calibration row')
        seen.add(key)
        candidates = row.get('candidates')
        if not isinstance(candidates, list): raise ValueError('candidate list missing')
        label = indexed[key]
        if not candidates:
            if label['candidate_id'] is not None or label['relevant']:
                raise ValueError('empty candidate annotation mismatch')
            samples[key[1]].append((None, False))
            continue
        candidate = candidates[0]
        if not isinstance(candidate, dict): raise ValueError('invalid candidate')
        candidate_id, score = candidate.get('id'), candidate.get('score')
        if (type(candidate_id) is not int or type(label['candidate_id']) is not int or
                candidate_id != label['candidate_id'] or type(score) not in (int, float) or not math.isfinite(score)):
            raise ValueError('candidate identity or score mismatch')
        samples[key[1]].append((float(score), label['relevant']))
    if seen != set(indexed): raise ValueError('annotation inventory mismatch')
    case_ids = calibration['identity'].get('case_ids')
    if (not isinstance(case_ids, list) or not case_ids or
            any(not isinstance(case_id, str) or not case_id for case_id in case_ids) or
            len(case_ids) != len(set(case_ids))):
        raise ValueError('frozen calibration case inventory missing')
    question_sets = [{eval_id for eval_id, strat in seen if strat == strategy} for strategy in STRATEGIES]
    if any(ids != set(case_ids) for ids in question_sets):
        raise ValueError('strategy question sets differ from frozen inventory')
    thresholds = {}
    for strategy, items in samples.items():
        positives = sum(label for _, label in items)
        negatives = len(items) - positives
        scores = sorted({score for score, _ in items if score is not None})
        if not positives or not negatives or not scores: raise ValueError('both relevance classes required')
        def objective(threshold):
            tp = sum(label and score is not None and score >= threshold for score, label in items)
            tn = sum(not label and (score is None or score < threshold) for score, label in items)
            return ((tp / positives + tn / negatives) / 2, threshold)
        thresholds[strategy] = max(scores + [math.nextafter(scores[-1], math.inf)], key=objective)
    return ConfidencePolicy(identity, thresholds)


def fit_confidence_policy(calibration):
    if calibration.get('complete') is not True or calibration.get('split')!='calibration':
        raise ValueError('a completed calibration report is required')
    rows=calibration.get('rows')
    if not isinstance(rows,list) or len(rows)!=80:
        raise ValueError('calibration must contain 80 strategy results')
    keys=[(r.get('eval_id'),r.get('strategy')) for r in rows]
    if len(keys)!=len(set(keys)):
        raise ValueError('duplicate calibration results')
    question_sets=[{r['eval_id'] for r in rows if r.get('strategy')==strategy} for strategy in STRATEGIES]
    if any(ids != question_sets[0] for ids in question_sets[1:]):
        raise ValueError('strategy question sets differ')
    identity=calibration.get('identity',{}).get('policy_identity')
    if not isinstance(identity,dict) or set(identity)!={'model','corpus_hash','prompt_hash','query_hash','strategy'}:
        raise ValueError('policy identity missing')
    thresholds={}
    for strategy in STRATEGIES:
        items=[r for r in rows if r.get('strategy')==strategy]
        if len(items)!=20 or Counter(r.get('bucket') for r in items)!={b:4 for b in ('A_policy','B_model','C_colloquial','D_missing','E_multi')}:
            raise ValueError('calibration bucket inventory invalid')
        samples=[]
        for row in items:
            if row.get('phase')!='complete' or row.get('status') not in ('answered','refused'):
                raise ValueError('calibration contains incomplete result')
            coverage=row.get('evidence_coverage')
            if type(row.get('answerable')) is not bool or (coverage is not None and
                    (type(coverage) not in (int,float) or not math.isfinite(coverage) or not 0 <= coverage <= 1)):
                raise ValueError('invalid calibration label or evidence coverage')
            if row['bucket']=='D_missing' and row['answerable']:
                raise ValueError('missing cases cannot be answerable')
            if row['status']=='answered' and not row.get('judge_completed',row.get('faithfulness') is not None):
                raise ValueError('answered calibration result lacks a judge')
            candidates=row.get('candidates')
            if not isinstance(candidates,list): raise ValueError('candidate list missing')
            score=candidates[0]['score'] if candidates else None
            if score is not None and (type(score) not in (int,float) or not math.isfinite(score)):
                raise ValueError('invalid candidate score')
            positive=bool(row.get('answerable') and row.get('evidence_coverage')==1.0)
            samples.append((score,positive))
        positives=sum(label for _,label in samples); negatives=len(samples)-positives
        if not positives or not negatives: raise ValueError('calibration needs both label classes')
        scores=sorted({float(score) for score,_ in samples if score is not None})
        if not scores: raise ValueError('no observed score for strategy')
        boundaries=scores+[math.nextafter(scores[-1],math.inf)]
        def score_threshold(threshold):
            true_positive=sum(label and score is not None and score>=threshold for score,label in samples)
            true_negative=sum(not label and (score is None or score<threshold) for score,label in samples)
            balanced=(true_positive/positives+true_negative/negatives)/2
            return balanced,true_positive,-threshold
        thresholds[strategy]=max(boundaries,key=score_threshold)
    return ConfidencePolicy(identity,thresholds)


async def validate_prompt_samples(cases, normalizer, rag_gateway, judge, bundle_factory, output, *, identity=None):
    path=Path(output)
    sample_hash=sha256(json.dumps(cases,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    run_identity={'samples_sha256':sample_hash,'environment':identity}
    state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'identity':run_identity,'results':[],'errors':[]}
    ids=[c['id'] for c in cases]
    if state.get('identity')!=run_identity or len(ids)!=len(set(ids)) or any(r['id'] not in ids for r in state['results']):
        raise ValueError('prompt sample identity mismatch')
    completed={r['id'] for r in state['results']}
    for case in cases:
        if case['id'] in completed: continue
        try:
            understanding=await normalizer(case['query'])
            bundle=bundle_factory(case,understanding)
            decision=await rag_gateway.answer(bundle)
            expected=case['expected_claim_supported']
            judged=await judge.judge(case['candidate_answer'],bundle.citations) if expected is not None else None
            judge_pass=(None if expected is None else bool(judged.claims) and
                        (all(c['supported'] for c in judged.claims) if expected else any(not c['supported'] for c in judged.claims)))
            self_pass=decision.useful is case['expected_useful']
            result={'id':case['id'],'query':case['query'],'expected_useful':case['expected_useful'],
                    'expected_claim_supported':expected,'annotation_reason':case['reason'],
                    'understanding':_plain(understanding),'decision':_plain(decision),
                    'candidate_answer':case['candidate_answer'],'judgement':_plain(judged) if judged is not None else None,
                    'self_check_pass':self_pass,'judge_pass':judge_pass,
                    'passed':self_pass and judge_pass is not False}
            state['results'].append(result)
            _write(path,state)
        except Exception as exc:
            code=getattr(exc,'code',None)
            state['errors'].append({'id':case['id'],'code':code if isinstance(code,str) else type(exc).__name__})
            _write(path,state)
            raise
    return {'results':state['results'],'errors':state['errors'],
            'passed':sum(r['passed'] for r in state['results']),'failed':sum(not r['passed'] for r in state['results'])}


def publish_policy(policy, output):
    output=Path(output)
    backup=None
    if output.exists():
        data=output.read_bytes()
        backup=output.with_name(output.name+'.backup-'+uuid.uuid4().hex)
        with backup.open('xb') as handle: handle.write(data)
        if backup.read_bytes()!=data: raise OSError('policy backup mismatch')
    _write(output,{'identity':policy.identity,'calibrated':True,'thresholds':policy.thresholds})
    return backup


def main(argv=None):
    parser=argparse.ArgumentParser(description='从完整校准报告拟合置信度门槛')
    parser.add_argument('--report',required=True)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--annotations',help='与报告身份及首位候选绑定的人工相关性标注')
    mode.add_argument('--legacy',action='store_true',help='复现历史按可回答性校准的旧方法，不建议用于新策略')
    parser.add_argument('--output',default='artifacts/knowledge/rag-confidence.json')
    args=parser.parse_args(argv)
    root=Path(__file__).resolve().parents[1]
    report_path=Path(args.report).resolve()
    output=Path(args.output).resolve()
    if not report_path.is_relative_to((root/'artifacts/evals/ch04').resolve()):
        raise ValueError('report must be under chapter-four evaluation artifacts')
    if not output.is_relative_to((root/'artifacts').resolve()):
        raise ValueError('policy must be under project artifacts')
    report=json.loads(report_path.read_text(encoding='utf-8'))
    if args.annotations:
        annotation_path=Path(args.annotations).resolve()
        if not annotation_path.is_relative_to((root/'artifacts/evals/ch04').resolve()):
            raise ValueError('annotations must be under chapter-four evaluation artifacts')
        annotations=json.loads(annotation_path.read_text(encoding='utf-8'))
        policy=fit_relevance_policy(report,annotations)
    else:
        checkpoint=json.loads((report_path.parent/'checkpoint.json').read_text(encoding='utf-8'))
        manifest=json.loads((report_path.parent/'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('status')!='completed' or report.get('identity')!=manifest.get('identity'):
            raise ValueError('calibration manifest is not complete or does not match report')
        report['rows']=checkpoint['rows']
        policy=fit_confidence_policy(report)
    backup=publish_policy(policy,output)
    print(json.dumps({'output':str(output),'backup':str(backup) if backup else None,'thresholds':policy.thresholds},ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
