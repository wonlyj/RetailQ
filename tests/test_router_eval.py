def test_malformed_router_output_stays_in_parse_rate_denominator():
    from scripts.evaluate_router import summarize
    rows=[{'observations':{'x':{'raw_content':'{}','phase':'parsed'}},'ok':True,'review':{}},
          {'observations':{'x':{'raw_content':'{}','phase':'parsed'}},'ok':True,'review':{}},
          {'observations':{'x':{'raw_content':'{bad','phase':'invalid'}},'ok':False,'review':{}}]
    result=summarize({'cases':rows,'expected_count':3})
    assert result['json_received']==3 and result['json_parsed']==2
    assert result['json_parse_rate']==2/3 and result['status']=='fail'


def test_unreviewed_semantics_cannot_pass():
    from scripts.evaluate_router import summarize
    assert summarize({'cases':[{'ok':True,'observations':{},'review':{}}],'expected_count':1})['status']=='pending'


def test_workflow_proof_does_not_trust_pass_boolean():
    from scripts.router_proof import validate_turn
    import pytest
    with pytest.raises(ValueError):validate_turn({'intent':'退款退货'}, {'ok':True,'events':[],'runtime':{}})


def test_scope_inventory_cannot_be_relabelled():
    from scripts.router_proof import validate_inventory
    import pytest
    with pytest.raises(ValueError):validate_inventory([{'id':'S01','intent':'其他'}],[{'id':'S01','intent':'退款退货'}])


def valid_evidence():
    import json
    from retailq.workflow.repository import digest
    final={'answer':'请说明零售相关问题。'}
    calls={name:{'call_id':name,'stage':name,'phase':'parsed','finish_reason':'stop','tool_calls':[],
                'raw_content':json.dumps(value,ensure_ascii=False)} for name,value in {
                    'resolve':{'resolved_question':'奇怪的问题'},'intent':{'intent':'其他','confidence':0.4}}.items()}
    return {'question':'奇怪的问题','streams':[[['meta',{'conversation_id':1,'turn_id':'t'}],
            ['token',{'text':final['answer']}],['actions',{'actions':[]}],['done',{}]]],
            'runtime':{'turn':{'turn_id':'t','conversation_id':1,'user_message_id':2,'final_message_id':3,'status':'completed',
                'payload':{'final':final,'final_hash':digest(final),'router_calls':calls,'router_audit':{'confidence':0.4}}},
                'messages':[{'id':2,'role':'user','content':'奇怪的问题','tool_calls':None},
                            {'id':3,'role':'assistant','content':final['answer'],'tool_calls':None}],'tickets':[]}}


def test_graph_stage_observations_bind_to_turn():
    import pytest
    from scripts.router_proof import validate_turn
    row=valid_evidence()
    assert validate_turn({'intent':'其他'},row)==(1,'t',2)
    row['runtime']['turn']['payload']['router_audit']['confidence']=0.9
    with pytest.raises(ValueError,match='classifier audit'):validate_turn({'intent':'其他'},row)


def test_manifest_changes_invalidate_current_acceptance():
    import pytest
    from scripts.router_proof import validate_manifest
    with pytest.raises(ValueError):validate_manifest({'a':'before'},{'a':'after'})


def test_proof_rejects_cross_turn_missing_sql_and_tokens():
    import copy,pytest
    from scripts.router_proof import validate_turn
    for mutate in (
        lambda r:r['runtime']['turn'].update(turn_id='another'),
        lambda r:r['runtime']['messages'].pop(),
        lambda r:r['streams'][0][1][1].update(text='uncommitted'),
        lambda r:r['runtime']['turn']['payload']['router_calls']['resolve'].update(raw_content='{bad'),
    ):
        row=copy.deepcopy(valid_evidence());mutate(row)
        with pytest.raises((ValueError,KeyError)):validate_turn({'intent':'其他'},row)


def valid_core_evidence():
    import json
    from retailq.workflow.repository import digest
    row=valid_evidence();turn=row['runtime']['turn'];p=turn['payload']
    p['router_calls']['intent']['raw_content']=json.dumps({'intent':'退款退货','confidence':0.4})
    p['router_calls']['plan']={'call_id':'plan','stage':'plan','phase':'parsed','finish_reason':'stop',
                             'tool_calls':[],'raw_content':'{"core_case":false}'}
    p['router_audit'].update(order_id='1001',query_plan={'core_case':False})
    p['final']['gate']={'allowed':False,'policy_hash':'a'*64,'strategy':'policy_single'}
    p['final_hash']=digest(p['final'])
    p['pending']={'interrupt_id':'i'}
    p['resume_claim']={'interrupt_id':'i','request_key':'r','selection':{'order_id':'1001'}}
    row['runtime']['messages'].extend([
        {'id':4,'role':'assistant','content':'','tool_calls':[{'id':'ord_t','function':{'arguments':'{"order_id":"1001"}'}}]},
        {'id':5,'role':'tool','content':'{"ok":true,"data":{"order_id":"1001"}}','tool_call_id':'ord_t','tool_calls':None}])
    meta=['meta',{'conversation_id':1,'turn_id':'t'}]
    row['streams']=[
        [meta,['order_selector',{'interrupt_id':'i'}],['paused',{}]],
        [meta,*[['workflow',{'phase':'enter','node':n}] for n in ('select_order','load_order','plan_query','retrieve_policy','gate')],
         *row['streams'][0][1:]]]
    row['pause_checkpoint']={'turn_id':'t','next':['select_order'],
                            'interrupts':[{'id':'i','value':{'turn_id':'t','conversation_id':1}}]}
    return row


def test_proof_requires_real_pause_resume_order_and_receipt():
    import copy,pytest
    from scripts.router_proof import validate_turn
    expected={'intent':'退款退货','core_case':False,'selector':'1001'}
    assert validate_turn(expected,valid_core_evidence())==(1,'t',2)
    for mutate in (
        lambda r:r.pop('pause_checkpoint'),
        lambda r:r['runtime']['turn']['payload'].pop('resume_claim'),
        lambda r:r['runtime']['messages'].pop(),
        lambda r:r['streams'][1].reverse(),
        lambda r:r['runtime']['turn']['payload']['router_calls']['plan'].update(raw_content='{"core_case":true}'),
    ):
        row=copy.deepcopy(valid_core_evidence());mutate(row)
        with pytest.raises((ValueError,KeyError)):validate_turn(expected,row)


def prompt_proof_fixture(monkeypatch):
    from scripts import evaluate_router as module
    case={'id':'one','question':'奇怪的问题','intent':'其他','verbatim':True}
    monkeypatch.setattr(module,'prompt_samples',lambda:[case])
    monkeypatch.setattr(module,'manifest',lambda:{'file':'sha'})
    return {'schema_version':2,'kind':'prompts','expected_count':1,'manifest':{'file':'sha'},'samples':[case],
            'cases':[{'id':'one','input':case,'ok':True,'actual':{'resolved_question':case['question'],'intent':'其他','confidence':0.4},
                      'observations':valid_evidence()['runtime']['turn']['payload']['router_calls'],
                      'review':{'status':'pass','reason':'已逐条核对'}}]}


def test_prompt_proof_reparses_raw_even_when_phase_says_parsed(monkeypatch):
    from scripts.evaluate_router import summarize
    report=prompt_proof_fixture(monkeypatch)
    assert summarize(report)['status']=='pass'
    report['cases'][0]['observations']['resolve']['raw_content']='{broken'
    result=summarize(report)
    assert result['status']=='fail' and result['json_parsed']==1 and result['json_received']==2


def test_prompt_proof_cannot_change_observed_intent(monkeypatch):
    from scripts.evaluate_router import summarize
    report=prompt_proof_fixture(monkeypatch)
    report['cases'][0]['observations']['intent']['raw_content']='{"intent":"物流","confidence":0.4}'
    assert summarize(report)['status']=='fail'


def test_prompt_proof_cannot_invent_resolution(monkeypatch):
    from scripts.evaluate_router import summarize
    report=prompt_proof_fixture(monkeypatch)
    report['cases'][0]['observations']['resolve']['raw_content']='{"resolved_question":"完全不同的问题"}'
    assert summarize(report)['status']=='fail'


def test_workflow_proof_requires_frozen_question():
    import pytest
    from scripts.router_proof import validate_turn
    with pytest.raises(ValueError):validate_turn({'question':'冻结的原问题','intent':'其他'},valid_evidence())


def test_workflow_multiturn_stays_same_conversation(monkeypatch):
    import copy
    from scripts import evaluate_router as module
    label={'question':'奇怪的问题','intent':'其他'}
    samples=[{'id':'one','turns':[label,label]}]
    monkeypatch.setattr(module,'workflow_samples',lambda:samples)
    monkeypatch.setattr(module,'manifest',lambda:{'file':'sha'})
    first=valid_evidence();second=copy.deepcopy(first)
    for row in (first,second):
        row['review']={'status':'pass','reason':'已核对'};row['runtime']['user_id']='alice'
    second['streams'][0][0][1].update(turn_id='t2')
    second['runtime']['turn'].update(turn_id='t2',user_message_id=4,final_message_id=5)
    second['runtime']['messages'][0]['id']=4;second['runtime']['messages'][1]['id']=5
    report={'schema_version':2,'kind':'workflow','manifest':{'file':'sha'},'samples':samples,
            'run_complete':True,'cases':[{'id':'one','user_id':'alice','turns':[first,second]}]}
    assert module.summarize(report)['status']=='pass'
    second['runtime']['user_id']='bob'
    assert module.summarize(report)['status']=='fail'
    second['runtime']['user_id']='alice'
    second['streams'][0][0][1]['conversation_id']=2
    second['runtime']['turn']['conversation_id']=2
    assert module.summarize(report)['status']=='fail'


def test_business_proof_requires_every_requested_order_receipt():
    import json,pytest
    from scripts.router_proof import validate_turn
    row=valid_evidence();question='订单1001和1002各是什么状态'
    row['question']=question;row['runtime']['messages'][0]['content']=question
    payload=row['runtime']['turn']['payload']
    payload['router_calls']['intent']['raw_content']='{"intent":"订单","confidence":0.4}'
    payload['router_calls']['resolve']['raw_content']=json.dumps({'resolved_question':question})
    expected={'question':question,'intent':'订单','read_order_ids':['1001','1002']}
    with pytest.raises(ValueError):validate_turn(expected,row)
    final=row['runtime']['messages'].pop();final['id']=7
    row['runtime']['turn']['final_message_id']=7
    for index,order in enumerate(('1001','1002')):
        call_id='read'+order
        row['runtime']['messages'].extend([
            {'id':3+index*2,'role':'assistant','tool_calls':[{'id':call_id,'function':{
                'name':'query_order','arguments':json.dumps({'order_id':order})}}]},
            {'id':4+index*2,'role':'tool','tool_calls':None,'tool_call_id':call_id,
             'content':json.dumps({'ok':True,'data':{'order_id':order,'status':'已发货'}})}])
    row['runtime']['messages'].append(final)
    assert validate_turn(expected,row)==(1,'t',2)
