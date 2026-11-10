"""Offline consistency checks from captured frames and committed SQL facts."""
from hashlib import sha256
import json
import re
from retailq.workflow.repository import digest
from retailq.errors import ServiceError
from retailq.workflow.router_contracts import parse_intent,parse_resolution,parse_queries,parse_query_plan


def validate_inventory(expected,observed):
    if expected!=observed:raise ValueError('frozen inventory differs')


def validate_manifest(expected,observed):
    if not expected or expected!=observed:raise ValueError('executable manifest differs')


def validate_turn(expected,row):
    if 'question' in expected and row.get('question')!=expected['question']:raise ValueError('question differs from frozen sample')
    streams=row.get('streams',[])
    if not streams:raise ValueError('missing stream evidence')
    metadata=[];nodes=[];answer='';terminal=None
    for events in streams:
        metas=[d for k,d in events if k=='meta']
        endings=[(k,d) for k,d in events if k in ('done','paused','cancelled','error')]
        if len(metas)!=1 or len(endings)!=1 or events[-1][0]!=endings[0][0] or endings[0][0]=='error':
            raise ValueError('incomplete or errored stream')
        metadata.append(metas[0]);terminal=endings[0][0]
        if terminal=='done' and sum(k=='actions' for k,_ in events)!=1:raise ValueError('missing actions frame')
        nodes.extend(d['node'] for k,d in events if k=='workflow' and d['phase']=='enter')
        answer+=''.join(d['text'] for k,d in events if k=='token')
    identity={(m['conversation_id'],m['turn_id']) for m in metadata}
    if len(identity)!=1:raise ValueError('resume changed turn')
    cid,turn_id=next(iter(identity))
    runtime=row['runtime'];turn=runtime['turn'];payload=turn['payload']
    if turn['turn_id']!=turn_id or turn['conversation_id']!=cid:raise ValueError('SQL/stream identity mismatch')
    users=[m for m in runtime['messages'] if m['role']=='user' and m['id']==turn['user_message_id']]
    if len(users)!=1 or users[0]['content']!=row['question']:raise ValueError('missing original user fact')
    observations=payload.get('router_calls',{})
    stages={}
    for call_id,observation in observations.items():
        if observation['call_id']!=call_id or observation.get('phase')!='parsed':raise ValueError('incomplete stage')
        stage=observation['stage'];stages.setdefault(stage,[]).append(observation)
        parsers={'intent':parse_intent,'resolve':parse_resolution,'expand':parse_queries,'plan':parse_query_plan}
        if stage not in parsers or observation.get('finish_reason')!='stop' or observation.get('tool_calls'):
            raise ValueError('invalid stage completion')
        try:parsers[stage](observation['raw_content'])
        except ServiceError as exc:raise ValueError('malformed stage completion') from exc
    if len(stages.get('intent',[]))!=1 or len(stages.get('resolve',[]))!=1:raise ValueError('duplicated or missing preparation')
    classification=parse_intent(stages['intent'][0]['raw_content']);intent=classification.intent
    if intent!=expected['intent']:raise ValueError('wrong current intent')
    audit=payload.get('router_audit',{})
    if terminal!='cancelled' and audit.get('confidence')!=classification.confidence:raise ValueError('classifier audit differs')
    if terminal=='cancelled':
        if turn['status']!='cancelled' or turn['final_message_id'] is not None:raise ValueError('cancel finalized an answer')
    else:
        final=payload.get('final',{})
        if not turn['final_message_id'] or digest(final)!=payload.get('final_hash'):raise ValueError('uncommitted final snapshot')
        if final['answer']!=answer:raise ValueError('token stream differs from committed answer')
        final_rows=[m for m in runtime['messages'] if m['id']==turn['final_message_id'] and m['role']=='assistant']
        if len(final_rows)!=1 or final_rows[0]['content']!=answer:raise ValueError('final SQL message missing')
    if expected.get('selector'):
        if not any(k=='paused' for events in streams for k,_ in events):raise ValueError('selector was bypassed')
        checkpoint=row.get('pause_checkpoint',{})
        if checkpoint.get('turn_id')!=turn_id or len(checkpoint.get('interrupts',[]))!=1:raise ValueError('missing durable interrupt')
        interruption=checkpoint['interrupts'][0];claim=payload.get('resume_claim',{});pending=payload.get('pending',{})
        frames=[d for events in streams for k,d in events if k=='order_selector']
        if (checkpoint.get('next')!=['select_order'] or len(streams)!=2 or len(frames)!=1
                or interruption['value'].get('turn_id')!=turn_id or interruption['value'].get('conversation_id')!=cid
                or pending.get('interrupt_id')!=interruption['id'] or claim.get('interrupt_id')!=interruption['id']
                or frames[0].get('interrupt_id')!=interruption['id'] or not claim.get('request_key')):
            raise ValueError('resume SQL/checkpoint identity differs')
        selection=claim.get('selection',{})
        if expected['selector']=='cancel':
            if selection.get('cancel') is not True:raise ValueError('cancel claim missing')
        elif selection.get('order_id')!=expected['selector']:raise ValueError('selected order differs')
    if intent in ('退款退货','售后') and terminal!='cancelled':
        required=['load_order','plan_query','retrieve_policy','gate']
        if any(n not in nodes for n in required) or [nodes.index(n) for n in required]!=sorted(nodes.index(n) for n in required):
            raise ValueError('core order/policy sequence violated')
        order_id=audit.get('order_id')
        calls=[c for m in runtime['messages'] for c in (m['tool_calls'] or []) if c.get('id')=='ord_'+turn_id]
        receipts=[m for m in runtime['messages'] if m['role']=='tool' and m['tool_call_id']=='ord_'+turn_id]
        if len(calls)!=1 or len(receipts)!=1:raise ValueError('missing deterministic order receipt')
        result=json.loads(receipts[0]['content'])
        if (not result.get('ok') or result['data']['order_id']!=order_id
                or json.loads(calls[0]['function']['arguments']).get('order_id')!=order_id):raise ValueError('order snapshot differs')
        is_core=audit.get('query_plan',{}).get('core_case')
        if len(stages.get('plan',[]))!=1 or parse_query_plan(stages['plan'][0]['raw_content']).core_case!=is_core:
            raise ValueError('query plan audit differs')
        if expected.get('core_case') is not None and is_core!=expected['core_case']:raise ValueError('wrong expansion plan')
        if (is_core and len(stages.get('expand',[]))!=1) or (not is_core and stages.get('expand')):raise ValueError('expansion invocation mismatch')
        gate=payload.get('final',{}).get('gate') or {}
        if not re.fullmatch(r'[0-9a-f]{64}',gate.get('policy_hash') or ''):raise ValueError('policy bytes not bound')
        if gate.get('allowed') is False and 'agent_decide' in nodes:raise ValueError('weak policy entered Agent')
        if 'agent_decide' in nodes and nodes.index('agent_decide')<nodes.index('gate'):raise ValueError('Agent ran before policy')
        citations=payload.get('final',{}).get('evidence',{}).get('citations',[])
        if gate.get('allowed') is True:
            types=payload['final']['evidence'].get('content_types',[])
            if not citations or not types or set(types)!={'policy'}:raise ValueError('mandatory policy evidence missing')
        documents=row.get('documents',[])
        for citation in citations:
            doc=next((d for d in documents if d['source_key']==citation['source_key'] and d['source_version']==citation['source_version']),None)
            if doc is None or (doc['source_key']!='review' and sha256(doc['text'].encode()).hexdigest()!=doc['source_version']):
                raise ValueError('source archive proof missing')
    if intent=='其他' and any(n in nodes for n in ('load_order','retrieve','retrieve_policy','tools')):
        raise ValueError('other intent executed business')
    if expected.get('read_order_ids'):
        tool_name='query_logistics' if intent=='物流' else 'query_order'
        found=set()
        for message in runtime['messages']:
            for call in message.get('tool_calls') or []:
                if call['function']['name']!=tool_name:continue
                order_id=json.loads(call['function']['arguments']).get('order_id')
                receipt=next((m for m in runtime['messages'] if m['role']=='tool' and m.get('tool_call_id')==call['id']),None)
                if receipt:
                    value=json.loads(receipt['content'])
                    if value.get('ok') is True and value.get('data',{}).get('order_id')==order_id:found.add(order_id)
        if not set(expected['read_order_ids'])<=found:raise ValueError('requested business receipts missing')
    if expected.get('refund_reason'):
        tickets=runtime['tickets'];confirmations=row.get('confirmations',[])
        if len(tickets)!=1 or len(confirmations)!=2 or confirmations[0]['ticket']['ticket_no']!=confirmations[1]['ticket']['ticket_no']:
            raise ValueError('refund not uniquely confirmed')
        if tickets[0]['ticket_type']!='退款' or expected['refund_reason'] not in tickets[0]['description']:
            raise ValueError('refund reason not bound')
    return cid,turn_id,turn['user_message_id']
