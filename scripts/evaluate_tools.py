"""Live chapter-2 evaluation: wire/DB checks plus explicit human semantic review."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import select

from retailq.config import Settings
from retailq.db import Database
from retailq.models import Ticket
from retailq.repositories import ChatRepository
from scripts.client import ApiFailure, chat
from scripts.evaluate import summarize_report

ROOT = Path(__file__).resolve().parents[1]


def score_tool_case(actual: dict, case: dict) -> dict[str,bool]:
    calls = actual.get('tool_calls',[])
    expected = case.get('expected_tool')
    checks = {'stream_complete':actual.get('finish_reason')=='stop' and bool(actual.get('text')),
              'selected_tool':len(calls)==(0 if expected is None else 1) and (expected is None or calls[0]['name']==expected)}
    if expected is not None:
        call = calls[0] if len(calls)==1 else {}
        result = call.get('result',{})
        data = result.get('data',{})
        checks['arguments'] = all(call.get('args',{}).get(k)==v for k,v in case.get('expected_args',{}).items())
        checks['tool_success'] = result.get('ok') is True
        if 'expected_found' in case:
            checks['faq_found'] = data.get('found') is case['expected_found']
            checks['faq_items'] = bool(data.get('items')) is case['expected_found']
        if expected in ('query_order','query_product','query_logistics'):
            checks['demo_source'] = data.get('source')=='demo' and '演示' in actual.get('text','')
            key = 'product_id' if expected=='query_product' else 'order_id'
            checks['id_in_answer'] = bool(data.get(key)) and data.get(key) in actual.get('text','')
            if expected in ('query_order','query_logistics'):
                checks['status_in_answer'] = bool(data.get('status')) and data['status'] in actual.get('text','')
        if expected=='create_ticket':
            number = data.get('ticket_no')
            checks['ticket_persisted'] = bool(number) and number in actual.get('persisted_ticket_nos',[])
            checks['ticket_in_answer'] = bool(number) and number in actual.get('text','')
    if case.get('check_setup_ticket'):
        number = actual.get('setup_ticket_no')
        checks['existing_ticket_recalled'] = bool(number) and number in actual.get('text','')
        checks['one_ticket_only'] = actual.get('persisted_ticket_nos') == [number]
    return checks


def attach_database_trace(repository: ChatRepository, actual: dict, user_id: str) -> dict:
    records = repository.load_messages(actual['conversation_id'],user_id)
    last_user = max(r.id for r in records if r.role=='user')
    results = {r.tool_call_id:json.loads(r.content) for r in records if r.role=='tool' and r.content}
    actual['tool_calls'] = [{'id':c['id'],'name':c['function']['name'],
        'args':json.loads(c['function']['arguments']),'result':results.get(c['id'],{})}
        for r in records if r.id>last_user for c in (r.tool_calls or [])]
    with repository.database.sessions() as session:
        actual['persisted_ticket_nos'] = list(session.scalars(select(Ticket.ticket_no).where(
            Ticket.conversation_id==actual['conversation_id']).order_by(Ticket.ticket_no)))
    actual['persisted_roles'] = [r.role for r in records]
    return actual


async def evaluate_tool_cases(base_url: str, output_dir: Path, *, case_ids: list[str]|None=None) -> dict:
    settings = Settings()
    cases = [json.loads(line) for line in (ROOT/'evals/ch02_cases.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    if case_ids is not None:
        cases = [case for case in cases if case['id'] in case_ids]
        if {case['id'] for case in cases} != set(case_ids):
            raise ValueError('unknown evaluation case')
    ids = [case['id'] for case in cases]
    if not ids or len(ids)!=len(set(ids)):
        raise ValueError('evaluation cases need unique IDs')
    report = {'schema_version':2,'created_at':datetime.now(timezone.utc).isoformat(),
        'configured_model':settings.openai_model,'model_identity_source':'local_config_not_independently_verified',
        'expected_case_ids':ids,'run_completed':False,'cases':[]}
    output_dir.mkdir(parents=True,exist_ok=True)
    def save():
        report.update(summarize_report(report))
        (output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    database = Database(settings) if settings.is_database_configured else None
    try:
        if database is None:
            report['unavailable']='database_not_configured'
            save()
            return report
        repository = ChatRepository(database)
        async with httpx.AsyncClient(trust_env=False,timeout=settings.model_timeout_seconds*2+20) as client:
            for case in cases:
                user_id = 'eval-ch02-'+uuid4().hex
                item = {'id':case['id'],'input':case,'rubric':case['rubric'],'review':{'status':'pending','reason':''}}
                try:
                    setup = None
                    if case.get('setup_message'):
                        setup = await chat(client,base_url,case['setup_message'],[],user_id=user_id)
                        attach_database_trace(repository,setup,user_id)
                    actual = await chat(client,base_url,case['message'],[],user_id=user_id,
                                        conversation_id=setup['conversation_id'] if setup else None)
                    attach_database_trace(repository,actual,user_id)
                    if setup:
                        item['setup_actual'] = setup
                        actual['setup_ticket_no'] = next(iter(setup['persisted_ticket_nos']),None)
                    item.update(actual=actual,auto_checks=score_tool_case(actual,case))
                except ApiFailure as exc:
                    item.update(error=exc.code,auto_checks={'api_success':False})
                    if exc.code in ('model_not_configured','database_not_configured'):
                        report['unavailable'] = exc.code
                except Exception:
                    # Local reports contain only public failures, never DB connection errors/credentials.
                    item.update(error='api_or_database_error',auto_checks={'api_success':False})
                report['cases'].append(item)
                save()
                if report.get('unavailable'):
                    return report
        report['run_completed'] = True
        save()
        return report
    finally:
        if database is not None:
            database.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url',default='http://127.0.0.1:8000')
    parser.add_argument('--output',type=Path,default=Path('artifacts/evals/ch02/run-01'))
    parser.add_argument('--review',type=Path)
    args = parser.parse_args()
    if args.review:
        report = json.loads(args.review.read_text(encoding='utf-8'))
        report.update(summarize_report(report))
        args.review.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    else:
        report = asyncio.run(evaluate_tool_cases(args.base_url,args.output))
    print(json.dumps(summarize_report(report),ensure_ascii=False,indent=2))
    return {'pass':0,'fail':1,'pending':2}[report['status']]


if __name__=='__main__':
    raise SystemExit(main())
