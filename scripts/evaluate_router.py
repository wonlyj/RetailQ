"""Frozen contextual-router samples; preserve raw completions including failures."""
import argparse
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import time

from langchain_core.messages import AIMessage,HumanMessage
from retailq.config import Settings
from retailq.gateway import OpenAIModelGateway,public_error
from retailq.workflow.classification import IntentClassifier
from retailq.workflow.query_processing import QuerySteps

ROOT=Path(__file__).resolve().parents[1]


def summarize(report):
    rows=report.get('cases',[])
    observations=[o for row in rows for o in row.get('observations',{}).values()]
    received=sum('raw_content' in o for o in observations)
    parsed=sum(o.get('phase')=='parsed' for o in observations)
    complete=len(rows)==report.get('expected_count') and bool(rows)
    failed=[r.get('id') for r in rows if not r.get('ok') or r.get('review',{}).get('status')=='fail']
    reviewed=all(r.get('review',{}).get('status')=='pass' and r.get('review',{}).get('reason','').strip() for r in rows)
    return {'status':'fail' if failed else 'pass' if complete and reviewed else 'pending',
            'complete':complete,'json_received':received,'json_parsed':parsed,
            'json_parse_rate':parsed/received if received else None,'failed_ids':failed}


def save(path,value):
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    temp.replace(path)


def new_output(output):
    target=Path(output).resolve();root=(ROOT/'artifacts/evals/ch06').resolve()
    if target==root or not target.is_relative_to(root) or target.exists():
        raise ValueError('Use a new directory under artifacts/evals/ch06')
    target.mkdir(parents=True)
    return target


def manifest():
    paths=[*ROOT.glob('retailq/**/*.py'),*ROOT.glob('retailq/prompts/workflow*.txt'),
           *ROOT.glob('scripts/*router*.py'),*ROOT.glob('evals/ch06*.jsonl'),ROOT/'requirements.lock']
    return {p.relative_to(ROOT).as_posix():sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


async def prompts(output,settings=None):
    settings=settings or Settings()
    if not settings.is_ready:raise ValueError('Configure the existing chat upstream first')
    target=new_output(output)
    samples=[json.loads(line) for line in (ROOT/'evals/ch06_router_cases.jsonl').read_text(encoding='utf-8').splitlines() if line]
    report={'schema_version':1,'kind':'prompts','manifest':manifest(),'samples':samples,
            'expected_count':len(samples),'cases':[],'model':settings.openai_model}
    gateway=OpenAIModelGateway(settings)
    try:
        for case in samples:
            row={'id':case['id'],'input':case,'observations':{},'review':{'status':'pending','reason':''},'ok':False}
            report['cases'].append(row)
            async def observer(value):
                key=value['call_id'];row['observations'][key]={**row['observations'].get(key,{}),**value}
                save(target/'report.json',report)
            messages=[(HumanMessage if m['role']=='user' else AIMessage)(content=m['content']) for m in case.get('history',[])]
            state={'raw_question':case['question'],'messages':[*messages,HumanMessage(content=case['question'])],'agent':{}}
            steps=QuerySteps(gateway,settings,deadline=time.monotonic()+settings.workflow_turn_timeout_seconds,observer=observer)
            try:
                state.update(await steps.resolve(state))
                state.update(await IntentClassifier(gateway,settings,observer=observer,deadline=steps.deadline).classify(state))
                if state['route']=='core':
                    state.update(await steps.plan(state))
                    if state['query_plan']['core_case']:state.update(await steps.expand(state))
                row['actual']={k:state.get(k) for k in ('resolved_question','intent','confidence','query_plan','queries')}
                row['ok']=state['intent']==case['intent'] and (not case['verbatim'] or state['resolved_question']==case['question'])
                if case.get('core_case') is not None:
                    row['ok'] &= state.get('query_plan',{}).get('core_case')==case['core_case']
            except Exception as exc:row['error_code']=public_error(exc).code
            report['summary']=summarize(report);save(target/'report.json',report)
    finally:await gateway.aclose()
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('prompts');p.add_argument('--output',required=True)
    p=sub.add_parser('review');p.add_argument('--report',required=True)
    args=parser.parse_args(argv)
    if args.command=='prompts':report=asyncio.run(prompts(args.output))
    else:report=json.loads(Path(args.report).read_text(encoding='utf-8'))
    result=summarize(report);print(json.dumps(result,ensure_ascii=False))
    return {'pass':0,'fail':1,'pending':2}[result['status']]


if __name__=='__main__':raise SystemExit(main())
