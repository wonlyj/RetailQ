"""Collect and calibrate the two chapter-six policy retrieval paths separately."""
import argparse
import asyncio
from hashlib import sha256
import json
import math
from pathlib import Path
import time

from retailq.config import Settings
from retailq.db import Database
from retailq.knowledge.answer import ConfidencePolicy
from retailq.knowledge.runtime import KnowledgeRuntime
from retailq.knowledge.workflow_policy import MODES,workflow_policy_identity,freeze_workflow_policy
from retailq.gateway import OpenAIModelGateway
from retailq.workflow.query_processing import QuerySteps
from scripts.evaluate_router import ROOT,new_output,save,manifest


def report_digest(report):
    return sha256(json.dumps(report,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def fit_policy(report,annotations,mode):
    if (report.get('split')!='calibration' or report.get('complete') is not True
            or annotations.get('report_sha')!=report_digest(report)):
        raise ValueError('completed calibration and bound annotations required')
    rows=[r for r in report['rows'] if r['mode']==mode]
    labels=[r for r in annotations['labels'] if r['mode']==mode]
    index={r['id']:r for r in labels}
    if len(rows)<12 or len(index)!=len(labels) or {r['id'] for r in rows}!=set(index):
        raise ValueError('calibration inventory differs')
    samples=[]
    for row in rows:
        label=index[row['id']];score=row['score']
        if (label.get('candidate_id')!=row['candidate_id'] or type(label.get('relevant')) is not bool
                or not label.get('reason','').strip() or (score is not None and (type(score) not in (float,int) or not math.isfinite(score)))):
            raise ValueError('invalid relevance annotation')
        if score is None and label['relevant']:raise ValueError('empty evidence cannot be relevant')
        samples.append((score,label['relevant']))
    positive=sum(label for _,label in samples);negative=len(samples)-positive
    if min(positive,negative)<4:raise ValueError('at least four positive and negative labels required')
    scores=sorted({float(score) for score,_ in samples if score is not None})
    if not scores:raise ValueError('no observed scores')
    def objective(threshold):
        tp=sum(label and score is not None and score>=threshold for score,label in samples)
        tn=sum(not label and (score is None or score<threshold) for score,label in samples)
        return ((tp/positive+tn/negative)/2,threshold)
    threshold=max([*scores,math.nextafter(scores[-1],math.inf)],key=objective)
    return ConfidencePolicy(report['identities'][mode],{mode:threshold})


def require_scope(settings):
    if (settings.mysql_host not in ('localhost','127.0.0.1') or settings.mysql_port!=3308
            or settings.mysql_database!='retailq_test' or settings.milvus_uri not in ('http://127.0.0.1:19531','http://localhost:19531')):
        raise ValueError('Use isolated 3308 retailq_test and Milvus19531')


async def collect(output,split='calibration',settings=None):
    settings=settings or Settings();require_scope(settings)
    target=new_output(output);database=Database(settings);runtime=None;gateway=None
    rows=[json.loads(line) for line in (ROOT/f'evals/ch06_policy_{split}.jsonl').read_text(encoding='utf-8').splitlines() if line]
    report={'split':split,'complete':False,'manifest':manifest(),'samples':rows,'rows':[],'identities':{}}
    try:
        runtime=KnowledgeRuntime.open(settings,database);gateway=OpenAIModelGateway(settings)
        snapshot=runtime.corpus.active()
        report['identities']={mode:workflow_policy_identity(settings,snapshot,mode) for mode in MODES}
        for case in rows:
            row={'id':case['id'],'mode':case['mode'],'question':case['question'],'observations':{}}
            report['rows'].append(row)
            async def observer(value):
                key=value['call_id'];row['observations'][key]={**row['observations'].get(key,{}),**value}
                save(target/'report.json',report)
            state={'resolved_question':case['question'],'agent':{}}
            deadline=time.monotonic()+settings.workflow_turn_timeout_seconds
            queries=[case['question']]
            if case['mode']=='policy_multi':
                queries=(await QuerySteps(gateway,settings,deadline=deadline,observer=observer).expand(state))['queries']
            result=await asyncio.to_thread(runtime.retrieve_policy,case['question'],queries=tuple(queries),mode=case['mode'],deadline=deadline)
            if result.snapshot.corpus_hash!=snapshot.corpus_hash:raise ValueError('active corpus changed; discard run')
            row.update(queries=queries,rerank_query=result.understanding.normalized_query,
                       candidate_id=result.candidates[0].id if result.candidates else None,
                       score=result.candidates[0].score if result.candidates else None,
                       candidates=[{'id':c.id,'score':c.score,'question':c.draft.questions,'text':c.draft.answer} for c in result.candidates])
            if split=='test':
                policy,policy_sha=freeze_workflow_policy(settings,result.snapshot,case['mode'])
                row.update(allowed=policy.allows(result),policy_sha=policy_sha,expected_allowed=case['expected_allowed'])
            save(target/'report.json',report)
        report['complete']=True
        if split=='test':report['passed']=all(r['allowed']==r['expected_allowed'] for r in report['rows'])
        save(target/'report.json',report)
        return report
    finally:
        if gateway:await gateway.aclose()
        if runtime:runtime.close()
        database.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    for name in ('collect','test'):
        p=sub.add_parser(name);p.add_argument('--output',required=True)
        if name=='test':p.add_argument('--policies',required=True)
    p=sub.add_parser('fit');p.add_argument('--report',required=True);p.add_argument('--annotations',required=True);p.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    if args.command=='fit':
        report=json.loads(Path(args.report).read_text(encoding='utf-8'));annotations=json.loads(Path(args.annotations).read_text(encoding='utf-8'))
        policies={mode:fit_policy(report,annotations,mode) for mode in MODES}
        target=new_output(args.output)
        for mode,policy in policies.items():save(target/(mode+'.json'),{'identity':policy.identity,'calibrated':True,'thresholds':policy.thresholds})
        print('Both policy modes calibrated; source report and annotations retained.');return 0
    settings=Settings()
    if args.command=='test':settings=settings.model_copy(update={f'workflow_{mode}_confidence_path':str(Path(args.policies)/(mode+'.json')) for mode in MODES})
    report=asyncio.run(collect(args.output,'test' if args.command=='test' else 'calibration',settings))
    print(json.dumps({'complete':report['complete'],'rows':len(report['rows']),'passed':report.get('passed')}))
    return 0 if report.get('passed',True) else 1


if __name__=='__main__':raise SystemExit(main())
