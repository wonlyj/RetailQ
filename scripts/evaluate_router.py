"""Frozen contextual-router samples; preserve raw completions including failures."""
import argparse
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import time
from uuid import uuid4

from langchain_core.messages import AIMessage,HumanMessage
from retailq.config import Settings
from retailq.gateway import OpenAIModelGateway,public_error
from retailq.workflow.classification import IntentClassifier
from retailq.workflow.query_processing import QuerySteps,effective_resolution,effective_queries
from retailq.workflow.router_contracts import parse_resolution,parse_intent,parse_query_plan,parse_queries
from retailq.errors import ServiceError

ROOT=Path(__file__).resolve().parents[1]


def parse_observations(observations):
    stages={};parsed=0;failed=False
    parsers={'resolve':parse_resolution,'intent':parse_intent,'plan':parse_query_plan,'expand':parse_queries}
    for call_id,value in observations.items():
        stage=value.get('stage')
        try:
            if ('raw_content' not in value or stage not in parsers or call_id!=value.get('call_id')
                    or value.get('finish_reason')!='stop' or value.get('tool_calls')):raise ValueError('invalid completion')
            result=parsers[stage](value['raw_content']);parsed+=1
            if value.get('phase')!='parsed' or stage in stages:raise ValueError('invalid stage receipt')
            stages[stage]=result
        except (KeyError,TypeError,ValueError,ServiceError):failed=True
    return stages,parsed,failed


def summarize(report):
    rows=report.get('cases',[])
    if report.get('kind')=='workflow':return summarize_workflow(report)
    observations=[o for row in rows for o in row.get('observations',{}).values()]
    received=sum('raw_content' in o for o in observations)
    parsed=sum(o.get('phase')=='parsed' for o in observations)
    complete=len(rows)==report.get('expected_count') and bool(rows)
    failed=[r.get('id') for r in rows if not r.get('ok') or r.get('review',{}).get('status')=='fail']
    reviewed=all(r.get('review',{}).get('status')=='pass' and r.get('review',{}).get('reason','').strip() for r in rows)
    if report.get('schema_version')==2 and report.get('kind')=='prompts':
        from scripts.router_proof import validate_inventory,validate_manifest
        try:
            validate_manifest(manifest(),report['manifest'])
            validate_inventory(prompt_samples(),report['samples'])
            if [r['id'] for r in rows]!=[c['id'] for c in report['samples']]:complete=False
            for row,case in zip(rows,report['samples']):
                if row.get('input')!=case:failed.append(row['id']);continue
                actual=row.get('actual',{})
                if actual.get('intent')!=case['intent'] or (case.get('verbatim',True) and actual.get('resolved_question')!=case['question']):failed.append(row['id'])
                stages,count,invalid=parse_observations(row.get('observations',{}))
                if invalid or set(stages) not in ({'resolve','intent'},{'resolve','intent','plan'},{'resolve','intent','plan','expand'}):
                    failed.append(row['id']);continue
                if (stages['intent'].intent!=actual.get('intent') or stages['intent'].confidence!=actual.get('confidence')):
                    failed.append(row['id'])
                if effective_resolution(case['question'],stages['resolve'])!=actual.get('resolved_question'):
                    failed.append(row['id'])
                core=case['intent'] in ('退款退货','售后')
                if ('plan' in stages)!=core:failed.append(row['id'])
                if core and 'plan' in stages:
                    plan=stages['plan'].core_case
                    if (actual.get('query_plan')!={'core_case':plan} or ('expand' in stages)!=plan
                            or (case.get('core_case') is not None and case['core_case']!=plan)):
                        failed.append(row['id'])
                    try:queries=effective_queries(actual.get('resolved_question'),stages['expand']) if plan else [actual.get('resolved_question')]
                    except (KeyError,ServiceError):failed.append(row['id']);continue
                    if actual.get('queries')!=queries:failed.append(row['id'])
        except (KeyError,ValueError,TypeError):failed.append('frozen_samples')
        parsed=sum(parse_observations(row.get('observations',{}))[1] for row in rows)
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


def prompt_samples():
    return [json.loads(line) for name in ('ch06_router_cases.jsonl','ch06_scope_cases.jsonl')
            for line in (ROOT/'evals'/name).read_text(encoding='utf-8').splitlines() if line]


async def prompts(output,settings=None):
    settings=settings or Settings()
    if not settings.is_ready:raise ValueError('Configure the existing chat upstream first')
    target=new_output(output)
    samples=prompt_samples()
    report={'schema_version':2,'kind':'prompts','manifest':manifest(),'samples':samples,
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
                row['ok']=state['intent']==case['intent'] and (not case.get('verbatim',True) or state['resolved_question']==case['question'])
                if case.get('core_case') is not None:
                    row['ok'] &= state.get('query_plan',{}).get('core_case')==case['core_case']
            except Exception as exc:row['error_code']=public_error(exc).code
            report['summary']=summarize(report);save(target/'report.json',report)
    finally:await gateway.aclose()
    return report


def workflow_samples():
    return [json.loads(line) for line in (ROOT/'evals/ch06_workflow_cases.jsonl').read_text(encoding='utf-8').splitlines() if line]


def summarize_workflow(report):
    from scripts.router_proof import validate_inventory,validate_turn,validate_manifest
    failures=[];pending=[];identities=[]
    cases=report.get('cases',[]);expected=workflow_samples()
    try:validate_inventory(expected,report['samples'])
    except (ValueError,KeyError,TypeError):failures.append('frozen_samples')
    try:validate_manifest(manifest(),report['manifest'])
    except (ValueError,KeyError,TypeError):failures.append('executable_manifest')
    complete=report.get('run_complete') is True and [c.get('id') for c in cases]==[c['id'] for c in expected]
    for case,spec in zip(cases,expected):
        case_identities=[]
        if not isinstance(case.get('user_id'),str) or not case['user_id']:failures.append(case.get('id','unknown')+':user')
        if len(case.get('turns',[]))!=len(spec['turns']):failures.append(case['id']);continue
        for actual,label in zip(case['turns'],spec['turns']):
            key=case['id']+':'+str(len(identities))
            try:
                identity=validate_turn(label,actual);identities.append(identity);case_identities.append(identity)
                if actual['runtime'].get('user_id')!=case.get('user_id'):raise ValueError('SQL owner differs from request user')
                gate=actual['runtime']['turn']['payload'].get('final',{}).get('gate') or {}
                if label['intent'] in ('退款退货','售后') and label.get('selector')!='cancel':
                    mode=gate.get('strategy')
                    if gate.get('policy_hash')!=report.get('policy_files',{}).get(mode):raise ValueError('policy file differs')
            except (ValueError,KeyError,TypeError,AttributeError,IndexError):failures.append(key)
            review=actual.get('review',{})
            if review.get('status')=='fail':failures.append(key)
            elif review.get('status')!='pass' or not review.get('reason','').strip():pending.append(key)
        if (len({i[0] for i in case_identities})!=1
                or [i[2] for i in case_identities]!=sorted(i[2] for i in case_identities)):
            failures.append(case['id']+':conversation_chain')
    case_cids=[c['turns'][0]['runtime']['turn']['conversation_id'] for c in cases if c.get('turns') and c['turns'][0].get('runtime')]
    if len(set(case_cids))!=len(case_cids):failures.append('reused_conversation')
    if len({identity[1] for identity in identities})!=len(identities) or len({identity[2] for identity in identities})!=len(identities):failures.append('reused_turn')
    return {'status':'fail' if failures else 'pass' if complete and not pending else 'pending','complete':complete,
            'workflow_cases':len(cases),'turns':len(identities),'failed_ids':failures,'pending_review':pending}


async def stream_request(client,path,body):
    events=[];event='';parts=[]
    async with client.stream('POST',path,json=body,timeout=180) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith(':'):continue
            if line.startswith('event:'):event=line[6:].strip()
            elif line.startswith('data:'):parts.append(line[5:].removeprefix(' '))
            elif not line and parts:
                if events and events[-1][0] in ('done','paused','cancelled','error'):raise ValueError('event after terminal')
                events.append([event,json.loads('\n'.join(parts))]);event='';parts=[]
    if not events or events[-1][0] not in ('done','paused','cancelled','error'):raise ValueError('stream lacks terminal')
    return events


def capture_runtime(database,turn_id,user_id):
    from sqlalchemy import select
    from retailq.models import Ticket,Conversation
    from retailq.repositories import ChatRepository
    from retailq.workflow.repository import WorkflowRepository
    turn=WorkflowRepository(database).get_turn(turn_id,user_id)
    messages=ChatRepository(database).load_messages(turn['conversation_id'],user_id)
    with database.sessions() as session:
        owner=session.get(Conversation,turn['conversation_id']).user_id
        tickets=session.scalars(select(Ticket).where(Ticket.conversation_id==turn['conversation_id'])).all()
        return {'turn':turn,'user_id':owner,'messages':[{'id':m.id,'role':m.role,'content':m.content,'tool_calls':m.tool_calls,'tool_call_id':m.tool_call_id}
            for m in messages if m.id>=turn['user_message_id']],
            'tickets':[{'ticket_no':t.ticket_no,'description':t.description,'ticket_type':t.ticket_type,'status':t.status} for t in tickets]}


async def capture_checkpoint(settings,identity,cid):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from retailq.workflow.graph import build_graph
    path=Path(settings.workflow_checkpoint_path)
    if not path.is_absolute():path=ROOT/path
    if not path.is_file():raise ValueError('server checkpoint file missing')
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        snapshot=await build_graph(None,None,None,None,saver).aget_state({'configurable':{'thread_id':identity+':'+str(cid)}})
        return {'turn_id':snapshot.values.get('turn_id'),'next':list(snapshot.next),
                'interrupts':[{'id':item.id,'value':item.value} for item in snapshot.interrupts]}


async def run_workflow(base_url,output,settings=None):
    import httpx
    from retailq.db import Database
    from scripts.calibrate_router import require_scope
    settings=settings or Settings();require_scope(settings)
    if base_url not in ('http://127.0.0.1:18806','http://localhost:18806'):raise ValueError('Use localhost18806')
    expected_identity=sha256(json.dumps([settings.mysql_host,settings.mysql_port,settings.mysql_database]).encode()).hexdigest()
    async with httpx.AsyncClient(base_url=base_url,trust_env=False,timeout=180) as client:
        preflight=await client.get('/api/workflow/identity');preflight.raise_for_status()
        if preflight.json()!={'database_identity':expected_identity}:raise ValueError('Wrong server database; no chat sent')
        target=new_output(output);database=Database(settings)
        report={'schema_version':2,'kind':'workflow','manifest':manifest(),'server_identity':expected_identity,
                'policy_files':{mode:sha256(Path(getattr(settings,'workflow_'+mode+'_confidence_path')).read_bytes()).hexdigest()
                                for mode in ('policy_single','policy_multi')},
                'samples':workflow_samples(),'cases':[],'run_complete':False}
        try:
            for case in report['samples']:
                result={'id':case['id'],'user_id':'ch06-'+case['id']+'-'+uuid4().hex[:12],'turns':[]};report['cases'].append(result)
                cid=None
                for label in case['turns']:
                    turn_id=None
                    row={'question':label['question'],'streams':[],'review':{'status':'pending','reason':''}};result['turns'].append(row)
                    try:
                        body={'message':label['question'],'user_id':result['user_id'],**({'conversation_id':cid} if cid else {})}
                        events=await stream_request(client,'/api/chat/stream',body);row['streams'].append(events)
                        meta=next(d for k,d in events if k=='meta');cid=meta['conversation_id'];turn_id=meta['turn_id']
                        if events[-1][0]=='paused':
                            row['pause_checkpoint']=await capture_checkpoint(settings,expected_identity,cid)
                            response=await client.get(f'/api/conversations/{cid}/pending',params={'user_id':result['user_id']})
                            response.raise_for_status();pending=response.json()['pending']
                            selection=label.get('selector')
                            if not selection:raise ValueError('unexpected order selector')
                            resume={'user_id':result['user_id'],'interrupt_id':pending['interrupt_id'],'request_key':uuid4().hex,
                                    **({'cancel':True} if selection=='cancel' else {'order_id':selection})}
                            row['streams'].append(await stream_request(client,f'/api/conversations/{cid}/turns/{turn_id}/resume',resume))
                        final_events=row['streams'][-1]
                        row['documents']=[]
                        for kind,data in final_events:
                            if kind=='sources':
                                for citation in data['citations']:
                                    response=await client.get(f'/api/conversations/{cid}/workflow-turns/{turn_id}/sources/{citation["number"]}/document',params={'user_id':result['user_id']})
                                    response.raise_for_status();row['documents'].append(response.json())
                        if label.get('refund_reason'):
                            actions=next(d['actions'] for k,d in final_events if k=='actions')
                            action=next(a for a in actions if a.get('subtype')=='refund')
                            confirm={'user_id':result['user_id'],'reason':label['refund_reason'],'request_key':uuid4().hex}
                            row['confirmations']=[]
                            for _ in range(2):
                                response=await client.post(f'/api/conversations/{cid}/actions/{action["action_id"]}/refund',json=confirm)
                                response.raise_for_status();row['confirmations'].append(response.json())
                        row['runtime']=await asyncio.to_thread(capture_runtime,database,turn_id,result['user_id'])
                    except Exception as exc:
                        row['error']=type(exc).__name__+':'+str(exc)[:250]
                        if cid and turn_id:
                            try:row['runtime']=await asyncio.to_thread(capture_runtime,database,turn_id,result['user_id'])
                            except Exception:pass
                    report['summary']=summarize_workflow(report);save(target/'report.json',report)
            report['run_complete']=True;report['summary']=summarize_workflow(report);save(target/'report.json',report)
        finally:database.close()
        return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('prompts');p.add_argument('--output',required=True)
    p=sub.add_parser('review');p.add_argument('--report',required=True)
    p=sub.add_parser('run');p.add_argument('--base-url',required=True);p.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    if args.command=='prompts':report=asyncio.run(prompts(args.output))
    elif args.command=='run':report=asyncio.run(run_workflow(args.base_url,args.output))
    else:report=json.loads(Path(args.report).read_text(encoding='utf-8'))
    result=summarize(report);print(json.dumps(result,ensure_ascii=False))
    return {'pass':0,'fail':1,'pending':2}[result['status']]


if __name__=='__main__':raise SystemExit(main())
