"""Offline structural evidence checks; semantic review remains explicit.

These checks reject missing/inconsistent proof, not maliciously fabricated files.
"""
from hashlib import sha256
import json
import re

from retailq.api.sources import public_citation
from retailq.workflow.repository import digest
from scripts.smoke_rag import verify_citation


def require(value):
    if not value:raise ValueError('incomplete or inconsistent workflow proof')


def validate_manifest(report,intents,cases):
    require(report.get('schema_version')==2)
    manifest=report['manifest'];files=manifest['files'];raw=manifest['sample_texts']
    required={'retailq/workflow/graph.py','retailq/workflow/classification.py','retailq/prompts/workflow_agent.txt',
        'retailq/prompts/workflow_intent.txt','scripts/evaluate_workflow.py','scripts/client.py','requirements.lock'}
    require(required<=set(files) and all(re.fullmatch(r'[0-9a-f]{64}',v) for v in files.values()))
    for name,expected in [('ch05_intents.jsonl',intents),('ch05_workflow_cases.jsonl',cases)]:
        text=raw[name]
        require(sha256(text.encode()).hexdigest()==files['evals/'+name])
        require([json.loads(line) for line in text.splitlines() if line.strip()]==expected)
    require(manifest['model'] and report['server_identity']=={
        'database_identity':sha256(json.dumps(['127.0.0.1',3308,'retailq_test']).encode()).hexdigest()})


def validate_turn(actual,proof,question,intent):
    require(actual['finish_reason']=='stop' and actual['text'] and proof['intent']==intent)
    cid,turn,mid=proof['conversation_id'],proof['turn_id'],proof['final_message_id']
    require(type(cid) is int and cid>0 and type(mid) is int and mid>0 and re.fullmatch(r'[0-9a-f]{32}',turn))
    require(actual['conversation_id']==cid and actual['meta']['conversation_id']==cid and actual['meta']['turn_id']==turn)
    final=proof['sql_final'];require(digest(final)==proof['sql_final_hash'])
    require(final['answer']==actual['text'] and final['status']=='completed')
    messages=proof['sql_messages'];require(messages and [m['id'] for m in messages]==sorted({m['id'] for m in messages}))
    require(messages[0]['role']=='user' and messages[0]['content']==question)
    require(messages[-1]['id']==mid and messages[-1]['role']=='assistant' and messages[-1]['content']==actual['text'])
    receipts={m['tool_call_id']:m for m in messages if m['role']=='tool'}
    calls=[]
    for m in messages:
        for c in m.get('tool_calls') or []:
            require(c.get('source')!='user_action')
            receipt=receipts[c['id']]
            require(messages[0]['id']<m['id']<receipt['id']<mid)
            calls.append({'id':c['id'],'name':c['function']['name'],'args':json.loads(c['function']['arguments']),
                'result':json.loads(receipt['content']),'request_message_id':m['id'],'receipt_message_id':receipt['id']})
    require(calls==proof['calls'] and len({c['id'] for c in calls})==len(calls))
    statuses=[e for e in actual['tool_status'] if e['status'] in ('completed','failed')]
    require([(s['tool_call_id'],s['name']) for s in statuses]==[(c['id'],c['name']) for c in calls])
    require(all(c['name'] in ('query_order','query_logistics','query_product') and
        s['status']==('completed' if c['result'].get('ok') else 'failed') for c,s in zip(calls,statuses)))
    envelope=actual['action_envelope']
    require(envelope['conversation_id']==cid and envelope['turn_id']==turn and envelope['message_id']==str(mid))
    require(envelope['actions']==actual['actions'])
    require(sorted(final['actions'] or [])==sorted(a['kind'] for a in actual['actions']))
    require(all(a['origin_assistant_message_id']==str(mid) for a in actual['actions']))
    entries=[(e['node'],e['turn_id']) for e in actual['workflow'] if e['phase']=='enter']
    require(entries==[(e['node'],e['turn_id']) for e in proof['trace'] if e['phase']=='enter'] and entries)
    require(all(t==turn for n,t in entries))
    require(proof['gate']==final.get('gate'))
    citations=(final.get('evidence') or {}).get('citations',[])
    require([public_citation(c).model_dump() for c in citations]==actual['sources'])
    if citations:
        source=actual['source_envelope']
        require(source['turn_id']==turn and source['message_id']==str(mid) and source['citations']==actual['sources'])
    return cid,turn,mid


def validate_case(case,row,auto_checks):
    require(row['question']==case['question'] and not row.get('error'))
    actual,proof=row['actual'],row['runtime_evidence']
    identity=validate_turn(actual,proof,case['question'],case['intent'])
    require(all(auto_checks(case,actual,proof).values()))
    require(row.get('auto_checks') and all(v is True for v in row['auto_checks'].values()))
    documents=row['source_documents']
    require([x['citation'] for x in documents]==actual['sources'])
    for entry in documents:
        citation,document=entry['citation'],entry['document'];verify_citation(citation,document)
        if citation['source_key']!='review':require(sha256(document['text'].encode()).hexdigest()==citation['source_version'])
    if case['intent']=='闲聊':
        from retailq.workflow.classification import SMALLTALK_REPLY
        require(actual['text']==SMALLTALK_REPLY)
    if 'confirmation' in case:
        first,second=row['confirmation_responses'];require(first==second==row['ticket_confirmation'] and first['status']=='completed')
        evidence=row['confirmation_evidence'];action=evidence['action'];tickets=evidence['tickets']
        suggested=next(a for a in actual['actions'] if a['kind']=='ticket')
        require(action['action_id']==suggested['action_id']==first['action_id'] and action['status']=='completed')
        require(action['origin_assistant_message_id']==proof['final_message_id'] and action['turn_id']==proof['turn_id'])
        require(action['request_hash']==digest(case['confirmation']) and action['result']==first['ticket'])
        require(len(tickets)==1 and tickets[0]['ticket_no']==first['ticket']['ticket_no'] and tickets[0]['conversation_id']==identity[0])
        require(all(tickets[0][k]==v for k,v in case['confirmation'].items()))
        follow=row['ticket_followup'];validate_turn(follow,row['ticket_followup_proof'],'刚才工单编号是什么？','售后')
        require(follow['conversation_id']==identity[0] and first['ticket']['ticket_no'] in follow['text'])
    return identity
