"""Aggregate completed rows without inventing scores for missing denominators."""
from collections import defaultdict
from statistics import mean

from retailq.knowledge.evaluation.runner import STRATEGIES


def _mean(values):
    values=[value for value in values if value is not None]
    return mean(values) if values else None


def _aggregate(rows):
    done=[r for r in rows if r.get('phase')=='complete']
    recalls=defaultdict(list)
    for row in done:
        for key,value in row.get('metrics',{}).get('recall',{}).items(): recalls[str(key)].append(value)
    claims=[c for row in done for c in row.get('claims',[])]
    supported=sum(c['supported'] for c in claims)
    answers=[r for r in done if r.get('status')=='answered']
    answerable=[r for r in done if r.get('answerable') is True and r.get('status') in ('answered','refused')]
    unanswerable=[r for r in done if r.get('answerable') is False and r.get('status') in ('answered','refused')]
    answerable_refusals=sum(r.get('status')=='refused' for r in answerable)
    unanswerable_refusals=sum(r.get('status')=='refused' for r in unanswerable)
    cited=sum(bool(r.get('citations')) for r in answers)
    judged=sum(r.get('judge_completed') is True for r in answers)
    latencies=[r['elapsed_seconds'] for r in done if not r.get('latency_incomplete') and r.get('elapsed_seconds') is not None]
    return {'rows':len(rows),'completed':len(done),'answered':len(answers),
            'refused':sum(r.get('status')=='refused' for r in done),
            'faults':sum(r.get('status')=='error' for r in done),
            'answerable_completed':len(answerable),'answerable_refusals':answerable_refusals,
            'answerable_refusal_rate':answerable_refusals/len(answerable) if answerable else None,
            'unanswerable_completed':len(unanswerable),'unanswerable_refusals':unanswerable_refusals,
            'unanswerable_refusal_rate':unanswerable_refusals/len(unanswerable) if unanswerable else None,
            'recall':{k:_mean(v) for k,v in recalls.items()},
            'mrr':_mean(r.get('metrics',{}).get('mrr') for r in done),
            'recall_at_50':_mean(r.get('candidate50_metrics',{}).get('recall',{}).get('50') for r in done),
            'full_evidence_coverage':_mean(r.get('multi_evidence_full_coverage') for r in done),
            'faithfulness_macro':_mean(r.get('faithfulness') for r in answers),
            'faithfulness_micro':supported/len(claims) if claims else None,
            'judge_claims':len(claims),'judge_supported':supported,
            'judge_answered':judged,'judge_coverage':judged/len(answers) if answers else None,
            'cited_answers':cited,'citation_coverage':cited/len(answers) if answers else None,
            'latency_mean_seconds':_mean(latencies),'latency_samples':len(latencies)}


def build_report(rows, *, split, expected):
    if split not in ('calibration','test') or expected < 0: raise ValueError('invalid report identity')
    keys=[(r['eval_id'],r['strategy']) for r in rows]
    if len(keys)!=len(set(keys)) or any(r['strategy'] not in STRATEGIES for r in rows):
        raise ValueError('duplicate or unknown evaluation strategy')
    by_strategy={s:[r for r in rows if r['strategy']==s] for s in STRATEGIES}
    buckets=sorted({r['bucket'] for r in rows})
    difficulties=sorted({r['difficulty'] for r in rows})
    return {'split':split,'expected':expected,'observed':len(rows),
            'complete':len(rows)==expected and all(r.get('phase')=='complete' for r in rows),
            'overall':{s:_aggregate(items) for s,items in by_strategy.items()},
            'by_bucket':{b:{s:_aggregate([r for r in items if r['bucket']==b]) for s,items in by_strategy.items()} for b in buckets},
            'by_difficulty':{d:{s:_aggregate([r for r in items if r['difficulty']==d]) for s,items in by_strategy.items()} for d in difficulties}}
