"""Synthetic proof-shaped fixtures for validator tests, never live evidence."""
import json
from hashlib import sha256
from retailq.config import Settings
from retailq.workflow.repository import digest
from retailq.workflow.classification import SMALLTALK_REPLY,COMPLAINT_REPLY
# Frozen chapter-five proof semantics; chapter-six routes must not relabel history.
ROUTES={'物流':'business','订单':'business','售后':'business','商品咨询':'knowledge',
        '退款退货':'knowledge','投诉':'complaint','闲聊':'smalltalk'}
from scripts.evaluate_workflow import samples,manifest,auto_checks


def fixture_turn(index,question,intent,names=(),kinds=(),cid=None):
    cid=cid or index;turn=f'{index:032x}';start=index*100;mid=start+99
    knowledge=ROUTES[intent]=='knowledge'
    text=SMALLTALK_REPLY if intent=='闲聊' else COMPLAINT_REPLY if intent=='投诉' else '受控演示回复[1]' if knowledge else '受控演示回复'
    citations=([{'number':1,'chunk_id':'1','relevance_rank':1,'category':'fixture','section_path':'审核知识',
        'questions':'fixture','text':'受控演示证据','source_key':'review','source_version':''}] if knowledge else [])
    stored=[{**c,'chunk_id':1,'archive':'','line_start':None,'line_end':None} for c in citations]
    actions=[{'kind':kind,'action_id':f'{index*10+i:032x}','status':'offered','origin_assistant_message_id':str(mid),'result':None} for i,kind in enumerate(kinds)]
    gate={'allowed':True,'policy_hash':'f'*64} if knowledge else None
    final={'answer':text,'status':'completed','actions':list(kinds),'gate':gate,'evidence':{'citations':stored} if knowledge else {}}
    messages=[{'id':start+1,'role':'user','content':question,'tool_calls':None,'tool_call_id':None}]
    calls=[];statuses=[]
    for j,name in enumerate(names):
        call_id=f'fixture_{index}_{j}';request_id=start+2+j*2;receipt_id=request_id+1
        args={'order_id':'1001'};result={'ok':True,'data':{'source':'demo','order_id':'1001','status':'fixture'}}
        wire={'id':call_id,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}
        messages.extend([{'id':request_id,'role':'assistant','content':None,'tool_calls':[wire],'tool_call_id':None},
            {'id':receipt_id,'role':'tool','content':json.dumps(result),'tool_calls':None,'tool_call_id':call_id}])
        calls.append({'id':call_id,'name':name,'args':args,'result':result,'request_message_id':request_id,'receipt_message_id':receipt_id})
        statuses.append({'name':name,'tool_call_id':call_id,'status':'completed'})
    messages.append({'id':mid,'role':'assistant','content':text,'tool_calls':None,'tool_call_id':None})
    nodes=['resolve','intent','route']
    if knowledge:nodes+=['retrieve','gate']
    if ROUTES[intent] in ('business','knowledge'):nodes+=['agent_decide']+['tools','agent_decide']*len(names)+['answer']
    else:nodes+=[ROUTES[intent]]
    nodes+=['log'];trace=[{'node':n,'phase':'enter','turn_id':turn} for n in nodes]
    envelope={'conversation_id':cid,'turn_id':turn,'message_id':str(mid),'actions':actions}
    actual={'conversation_id':cid,'meta':{'conversation_id':cid,'turn_id':turn},'text':text,'finish_reason':'stop',
        'actions':actions,'action_envelope':envelope,'sources':citations,'tool_status':statuses,'workflow':trace,
        'source_envelope':{'turn_id':turn,'message_id':str(mid),'citations':citations} if knowledge else None}
    proof={'conversation_id':cid,'turn_id':turn,'final_message_id':mid,'sql_final_hash':digest(final),'sql_final':final,
        'sql_messages':messages,'trace':trace,'intent':intent,'route':ROUTES[intent],'gate':gate,'calls':calls,'tickets_before_confirmation':[]}
    return actual,proof


def complete_report():
    intents=samples('ch05_intents.jsonl');cases=samples('ch05_workflow_cases.jsonl')
    report={'schema_version':2,'manifest':manifest(Settings(_env_file=None,openai_model='controlled-fixture')),
        'server_identity':{'database_identity':sha256(json.dumps(['127.0.0.1',3308,'retailq_test']).encode()).hexdigest()},
        'expected_intent_ids':[x['id'] for x in intents],'expected_case_ids':[x['id'] for x in cases],
        'run_completed':True,'intent_cases':[{'id':x['id'],'question':x['question'],'expected':x['intent'],'actual':x['intent']} for x in intents],'cases':[]}
    for i,case in enumerate(cases,1):
        kinds=case.get('expected_actions',[])
        actual,proof=fixture_turn(i,case['question'],case['intent'],case.get('expected_tools',[]),kinds)
        row={'id':case['id'],'question':case['question'],'actual':actual,'runtime_evidence':proof,
            'auto_checks':auto_checks(case,actual,proof),'review':{'status':'pending','reason':''},
            'source_documents':[{'citation':c,'document':{'source_key':'review','source_version':'','section_path':'审核知识','text':c['text']}} for c in actual['sources']]}
        if 'confirmation' in case:
            action=next(a for a in actual['actions'] if a['kind']=='ticket');ticket={'ticket_no':'T_FIXTURE','ticket_type':'投诉','status':'待处理'}
            result={'action_id':action['action_id'],'status':'completed','ticket':ticket}
            row.update(ticket_confirmation=result,confirmation_responses=[result,result],confirmation_evidence={
                'action':{**action,'turn_id':proof['turn_id'],'origin_assistant_message_id':proof['final_message_id'],
                    'request_hash':digest(case['confirmation']),'result':ticket,'status':'completed'},
                'tickets':[{**ticket,**case['confirmation'],'conversation_id':proof['conversation_id']}]})
            follow,follow_proof=fixture_turn(i+100,'刚才工单编号是什么？','售后',cid=proof['conversation_id'])
            follow['text']='T_FIXTURE';follow_proof['sql_messages'][-1]['content']='T_FIXTURE';follow_proof['sql_final']['answer']='T_FIXTURE'
            follow_proof['sql_final_hash']=digest(follow_proof['sql_final'])
            row.update(ticket_followup=follow,ticket_followup_proof=follow_proof)
        report['cases'].append(row)
    return report
