"""Frozen ch05 samples, actual workflow/SQL receipts, then explicit semantic review."""
import argparse
import asyncio
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from langchain_core.messages import HumanMessage,SystemMessage
from sqlalchemy import select
from retailq.config import Settings
from retailq.db import Database
from retailq.gateway import OpenAIModelGateway
from retailq.models import Ticket,Message
from retailq.repositories import ChatRepository
from retailq.workflow.repository import WorkflowRepository
from retailq.errors import ServiceError
from scripts.client import ApiFailure,chat,check_response
from scripts.smoke_rag import verify_citation
from scripts.workflow_proof import validate_manifest,validate_case

ROOT=Path(__file__).resolve().parents[1]


def samples(name):return [json.loads(x) for x in (ROOT/'evals'/name).read_text(encoding='utf-8').splitlines() if x.strip()]


def summarize(report):
    intents=samples('ch05_intents.jsonl');cases=samples('ch05_workflow_cases.jsonl')
    expected_i=[r['id'] for r in intents];expected_c=[r['id'] for r in cases]
    rows=report.get('cases',[]);classified=report.get('intent_cases',[])
    complete=(report.get('run_completed') is True and report.get('expected_intent_ids')==expected_i and
        report.get('expected_case_ids')==expected_c and [r.get('id') for r in rows]==expected_c and
        [r.get('id') for r in classified]==expected_i)
    failures=[];pending=[];identities=[]
    try:validate_manifest(report,intents,cases)
    except (KeyError,TypeError,ValueError):failures.append('manifest')
    correct=0
    for row,expected in zip(classified,intents):
        if row.get('question')!=expected['question'] or row.get('expected')!=expected['intent'] or row.get('actual')!=expected['intent']:
            failures.append(row.get('id'))
        else:correct+=1
    for row,expected in zip(rows,cases):
        try:identities.append(validate_case(expected,row,auto_checks))
        except (KeyError,TypeError,ValueError,StopIteration):failures.append(row.get('id'))
        review=row.get('review',{})
        if review.get('status')=='fail':failures.append(row.get('id'))
        elif review.get('status')!='pass' or not str(review.get('reason','')).strip():pending.append(row.get('id'))
    if any(len({identity[i] for identity in identities})!=len(identities) for i in range(3)):failures.append('reused_identity')
    status='fail' if failures else ('pass' if complete and not pending else 'pending')
    return {'status':status,'complete':complete,'intent_correct':correct,
            'intent_total':len(expected_i),'workflow_total':len(expected_c),'failed_ids':sorted(set(failures)),
            'pending_review':pending}


def validate_run_scope(settings,base_url,output):
    parsed=urlsplit(base_url);target=Path(output).resolve();root=(ROOT/'artifacts/evals/ch05').resolve()
    if (parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.port!=18805 or
        parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment or
        settings.mysql_host not in ('127.0.0.1','localhost') or settings.mysql_port!=3308 or settings.mysql_database!='retailq_test'
        or not target.is_relative_to(root) or target==root or target.exists()):
        raise ValueError('Use localhost:18805, existing isolated 3308/retailq_test, and a new artifacts/evals/ch05 directory.')
    return target


def manifest(settings):
    paths=[*ROOT.glob('retailq/**/*.py'),*ROOT.glob('retailq/prompts/*.txt'),*ROOT.glob('retailq/web/*.js'),
           *ROOT.glob('scripts/*.py'),*ROOT.glob('evals/ch05*.jsonl'),ROOT/'requirements.lock']
    return {'files':{p.relative_to(ROOT).as_posix():sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))},
            'git_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'model':settings.openai_model,'model_identity_source':'local_configuration_not_provider_verified',
            'sample_texts':{name:(ROOT/'evals'/name).read_bytes().decode('utf-8') for name in ('ch05_intents.jsonl','ch05_workflow_cases.jsonl')}}


def runtime_evidence(database,actual,user_id,question):
    turn_id=actual['meta']['turn_id'];repo=WorkflowRepository(database)
    turn=repo.get_turn(turn_id,user_id)
    if turn['conversation_id']!=actual['conversation_id']:raise ValueError('conversation mismatch')
    records=ChatRepository(database).load_messages(turn['conversation_id'],user_id)
    user=next(r for r in records if r.id==turn['user_message_id'])
    if user.content!=question or not turn['final_message_id']:raise ValueError('unconfirmed database answer')
    records=[r for r in records if turn['user_message_id']<=r.id<=turn['final_message_id']]
    receipts={r.tool_call_id:r for r in records if r.role=='tool'}
    calls=[{'id':c['id'],'name':c['function']['name'],'args':json.loads(c['function']['arguments']),
            'result':json.loads(receipts[c['id']].content) if c['id'] in receipts else None,
            'request_message_id':r.id,'receipt_message_id':receipts[c['id']].id if c['id'] in receipts else None} for r in records if r.id>turn['user_message_id']
            for c in (r.tool_calls or []) if c.get('source')!='user_action']
    with database.sessions() as session:
        tickets=list(session.scalars(select(Ticket.ticket_no).where(Ticket.conversation_id==turn['conversation_id'])))
    return {'conversation_id':turn['conversation_id'],'turn_id':turn_id,'final_message_id':turn['final_message_id'],
            'sql_final_hash':turn['payload']['final_hash'],'intent':turn['payload']['intent'],'route':turn['payload']['route'],
            'sql_final':turn['payload']['final'],'trace':turn['payload']['trace'],
            'sql_messages':[{key:getattr(r,key) for key in ('id','role','content','tool_calls','tool_call_id')} for r in records],
            'gate':turn['payload']['final'].get('gate'),'usage':turn['payload'].get('usage'),
            'calls':calls,'tickets_before_confirmation':tickets}


def auto_checks(case,actual,proof):
    nodes=[x['node'] for x in actual['workflow'] if x['phase']=='enter']
    calls=proof['calls'];names=[c['name'] for c in calls]
    checks={'stream_complete':actual['finish_reason']=='stop','intent':proof['intent']==case['intent'],
            'no_automatic_ticket':not proof['tickets_before_confirmation'],
            'allowed_agent_tools':all(n in ('query_order','query_product','query_logistics') for n in names),
            'tool_results_persisted':all(c['result'] is not None for c in calls)}
    if 'expected_tools' in case:checks['expected_tool_sequence']=names==case['expected_tools']
    if 'required_nodes' in case:checks['required_nodes']=all(n in nodes for n in case['required_nodes'])
    if 'forbidden_nodes' in case:checks['forbidden_nodes']=not set(nodes).intersection(case['forbidden_nodes'])
    if 'forbidden_tools' in case:checks['forbidden_tools']=not set(names).intersection(case['forbidden_tools'])
    if 'expected_actions' in case:checks['independent_actions']=sorted(a['kind'] for a in actual['actions'])==sorted(case['expected_actions'])
    if proof['gate'] is not None:checks['actual_policy_hash_recorded']=bool(proof['gate'].get('policy_hash'))
    if names:checks['demo_label']='演示' in actual['text']
    return checks


async def evaluate(base_url,output,settings=None):
    settings=settings or Settings();output=validate_run_scope(settings,base_url,output)
    expected_database=sha256(json.dumps(['127.0.0.1',3308,'retailq_test']).encode()).hexdigest()
    async with httpx.AsyncClient(trust_env=False,timeout=10) as preflight:
        response=await preflight.get(base_url.rstrip('/')+'/api/workflow/identity');check_response(response)
        server_identity=response.json()
        if server_identity!={'database_identity':expected_database}:raise ValueError('Server database is not the isolated 3308 acceptance database; no chat sent.')
    output.mkdir(parents=True)
    intents=samples('ch05_intents.jsonl');cases=samples('ch05_workflow_cases.jsonl')
    report={'schema_version':2,'created_at':datetime.now(timezone.utc).isoformat(),'manifest':manifest(settings),
            'server_identity':server_identity,
            'expected_intent_ids':[r['id'] for r in intents],'expected_case_ids':[r['id'] for r in cases],
            'run_completed':False,'intent_cases':[],'cases':[]}
    def save():
        report['summary']=summarize(report)
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    save();database=Database(settings);gateway=OpenAIModelGateway(settings)
    from retailq.workflow.classification import IntentClassifier
    classifier=IntentClassifier(gateway,settings)
    try:
        async with httpx.AsyncClient(trust_env=False,timeout=150) as client:
            for case in intents:
                row={'id':case['id'],'question':case['question'],'expected':case['intent'],'actual':None}
                try:
                    classified=await classifier.classify({'raw_question':case['question'],'messages':[HumanMessage(content=case['question'])]})
                    row.update(actual=classified['intent'],usage=classified['agent']['usage'])
                except Exception as exc:row['error']=getattr(exc,'code',type(exc).__name__)
                report['intent_cases'].append(row);save();print(f"intent {case['id']}: {row['actual']}",flush=True)
            for case in cases:
                user_id='ch05-'+uuid4().hex
                row={'id':case['id'],'question':case['question'],'user_id':user_id,'review':{'status':'pending','reason':''},'auto_checks':{}}
                try:
                    actual=await chat(client,base_url,case['question'],[],user_id=user_id)
                    proof=await asyncio.to_thread(runtime_evidence,database,actual,user_id,case['question'])
                    row.update(actual=actual,runtime_evidence=proof,auto_checks=auto_checks(case,actual,proof),user_id=user_id)
                    anchors=[]
                    for citation in actual['sources']:
                        url=f"{base_url}/api/conversations/{actual['conversation_id']}/workflow-turns/{actual['meta']['turn_id']}/sources/{citation['number']}/document"
                        response=await client.get(url,params={'user_id':user_id});check_response(response)
                        document=response.json();verify_citation(citation,document)
                        anchors.append({'citation':citation,'document':document})
                    row['source_documents']=anchors
                    if anchors:row['auto_checks']['source_documents_verified']=True
                    if case.get('check')=='fixed_smalltalk_one_classification':
                        from retailq.workflow.classification import SMALLTALK_REPLY
                        row['auto_checks']['fixed_reply']=actual['text']==SMALLTALK_REPLY
                    if 'confirmation' in case:
                        action=next(a for a in actual['actions'] if a['kind']=='ticket')
                        url=f"{base_url}/api/conversations/{actual['conversation_id']}/actions/{action['action_id']}/ticket"
                        body={'user_id':user_id,**case['confirmation']}
                        first=await client.post(url,json=body);check_response(first)
                        second=await client.post(url,json=body);check_response(second)
                        result=first.json();row['ticket_confirmation']=result
                        row['confirmation_responses']=[result,second.json()]
                        row['auto_checks']['confirmation_idempotent']=result==second.json() and result['status']=='completed'
                        follow=await chat(client,base_url,'刚才工单编号是什么？',[],user_id=user_id,conversation_id=actual['conversation_id'])
                        row['ticket_followup']=follow
                        row['ticket_followup_proof']=await asyncio.to_thread(runtime_evidence,database,follow,user_id,'刚才工单编号是什么？')
                        row['auto_checks']['ticket_fact_in_next_turn']=result['ticket']['ticket_no'] in follow['text']
                        with database.sessions() as session:
                            from retailq.workflow.models import WorkflowAction
                            saved_action=session.get(WorkflowAction,action['action_id'])
                            saved_tickets=session.scalars(select(Ticket).where(Ticket.conversation_id==actual['conversation_id'])).all()
                            tickets=[t.ticket_no for t in saved_tickets]
                            row['confirmation_evidence']={'action':{key:getattr(saved_action,key) for key in
                                ('action_id','turn_id','origin_assistant_message_id','request_hash','status','result')},
                                'tickets':[{key:getattr(t,key) for key in ('ticket_no','conversation_id','description','ticket_type','status')} for t in saved_tickets]}
                        row['auto_checks']['one_confirmed_ticket']=tickets==[result['ticket']['ticket_no']]
                except Exception as exc:row['error']=getattr(exc,'code',type(exc).__name__);row['auto_checks']['execution']=False
                report['cases'].append(row);save();print(f"workflow {case['id']}: {row.get('error') or row['auto_checks']}",flush=True)
            report['run_completed']=True;save()
    finally:await gateway.aclose();database.close()
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run');run.add_argument('--base-url',default='http://127.0.0.1:18805');run.add_argument('--output',required=True)
    review=sub.add_parser('review');review.add_argument('--report',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='run':report=asyncio.run(evaluate(args.base_url,args.output))
        else:report=json.loads(Path(args.report).read_text(encoding='utf-8'))
    except (ValueError,OSError,ApiFailure,ServiceError,httpx.HTTPError) as exc:
        print(json.dumps({'status':'pending','error':getattr(exc,'code',type(exc).__name__)},ensure_ascii=False))
        return 2
    summary=summarize(report);print(json.dumps(summary,ensure_ascii=False,indent=2))
    return {'pass':0,'fail':1,'pending':2}[summary['status']]


if __name__=='__main__':raise SystemExit(main())
