from copy import deepcopy
import json
from pathlib import Path
import pytest


from tests.support.workflow_proof import complete_report


def test_unreviewed_real_model_output_is_pending(tmp_path):
    from scripts.evaluate_workflow import main,summarize
    report=complete_report();path=tmp_path/'report.json';path.write_text(json.dumps(report),encoding='utf-8')
    assert summarize(report)['status']=='pending'
    assert main(['review','--report',str(path)])==2


@pytest.mark.parametrize('mutation',['missing','duplicate','db_missing','not_completed','wrong_intent','wrong_tool'])
def test_incomplete_or_unproven_report_cannot_pass(mutation):
    from scripts.evaluate_workflow import summarize
    report=complete_report()
    for row in report['cases']:row['review']={'status':'pass','reason':'人工核对实际事实'}
    if mutation=='missing':report['cases'].pop()
    if mutation=='duplicate':report['cases'][1]=deepcopy(report['cases'][0])
    if mutation=='db_missing':report['cases'][0]['runtime_evidence']={}
    if mutation=='not_completed':report['run_completed']=False
    if mutation=='wrong_intent':report['intent_cases'][0]['actual']='闲聊'
    if mutation=='wrong_tool':report['cases'][0]['auto_checks']['route']=False
    assert summarize(report)['status']!='pass'


def test_thin_repeated_identity_report_is_not_proof_of_execution():
    from scripts.evaluate_workflow import summarize
    report=complete_report()
    for row in report['cases']:row['review']={'status':'pass','reason':'x'}
    report.pop('manifest')
    for row in report['cases']:
        row.pop('actual')
        row['runtime_evidence']={'conversation_id':1,'turn_id':'a'*32,'final_message_id':2,'sql_final_hash':'f'*64}
    assert summarize(report)['status']!='pass'


def test_complete_consistent_controlled_proof_can_be_reviewed():
    from scripts.evaluate_workflow import summarize
    report=complete_report()
    for row in report['cases']:row['review']={'status':'pass','reason':'controlled validator fixture, not real-model quality evidence'}
    assert summarize(report)['status']=='pass'


@pytest.mark.parametrize('mutation',['snapshot','receipt','frame','manifest','source','identity'])
def test_frozen_proof_is_recomputed_instead_of_trusting_booleans(mutation):
    from scripts.evaluate_workflow import summarize
    report=complete_report()
    for row in report['cases']:row['review']={'status':'pass','reason':'controlled fixture'}
    if mutation=='snapshot':report['cases'][0]['runtime_evidence']['sql_final']['answer']='changed'
    if mutation=='receipt':report['cases'][1]['runtime_evidence']['sql_messages'].pop(2)
    if mutation=='frame':report['cases'][2]['actual']['action_envelope']['turn_id']='b'*32
    if mutation=='manifest':report['manifest']['sample_texts']['ch05_intents.jsonl']='[]'
    if mutation=='source':report['cases'][0]['source_documents']=[]
    if mutation=='identity':report['cases'][1]['runtime_evidence']=deepcopy(report['cases'][0]['runtime_evidence'])
    assert summarize(report)['status']=='fail'


def test_report_cannot_relabel_the_frozen_intent_answer():
    from scripts.evaluate_workflow import summarize
    report=complete_report()
    for row in report['cases']:row['review']={'status':'pass','reason':'x'}
    report['intent_cases'][0].update(expected='闲聊',actual='闲聊')
    assert summarize(report)['status']!='pass'


def test_scope_and_output_guard_refuse_overwrite_or_business_database(tmp_path):
    from scripts.evaluate_workflow import validate_run_scope
    from retailq.config import Settings
    settings=Settings(_env_file=None,mysql_port=3308,mysql_database='retailq_test')
    with pytest.raises(ValueError):validate_run_scope(settings,'http://example.com:18805',tmp_path/'new')
    with pytest.raises(ValueError):validate_run_scope(Settings(_env_file=None),'http://127.0.0.1:18805',tmp_path/'new')


@pytest.mark.anyio
async def test_collector_checks_server_database_before_model_or_chat(monkeypatch,tmp_path):
    import httpx
    from scripts import evaluate_workflow as module
    from retailq.config import Settings
    requests=[]
    def handler(request):
        requests.append((request.method,request.url.path))
        return httpx.Response(200,json={'database_identity':'wrong-database'})
    factory=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:factory(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(module,'validate_run_scope',lambda s,b,o:tmp_path/'run')
    def forbidden(*args):pytest.fail('model must not initialize before database identity admission')
    monkeypatch.setattr(module,'OpenAIModelGateway',forbidden)
    with pytest.raises(ValueError):await module.evaluate('http://127.0.0.1:18805',tmp_path/'run',Settings(_env_file=None))
    assert requests==[('GET','/api/workflow/identity')]


@pytest.mark.anyio
async def test_real_policy_gate_binds_the_exact_loaded_policy_bytes(monkeypatch,tmp_path):
    from retailq.workflow.evidence import EvidenceSteps
    from retailq.knowledge.policy import DynamicConfidencePolicy
    from retailq.config import Settings
    from langchain_core.messages import HumanMessage
    from tests.test_rag_answer import retrieval
    policy_path=tmp_path/'policy.json';policy_path.write_bytes(b'{"frozen":"bytes"}')
    settings=Settings(_env_file=None,rag_confidence_path=str(policy_path),context_window_tokens=20000)
    class Frozen:
        def allows(self,result):return True
    import retailq.knowledge.policy as policy_module
    monkeypatch.setattr(policy_module,'_policy_from_bytes',lambda raw,settings,snapshot:Frozen())
    steps=EvidenceSteps(lambda q:retrieval(1),DynamicConfidencePolicy(settings),settings)
    state={'raw_question':'问题','messages':[HumanMessage(content='问题')]}
    state={**state,**await steps.retrieve(state)}
    patch=await steps.gate(state)
    from hashlib import sha256
    assert patch['gate']['policy_hash']==sha256(b'{"frozen":"bytes"}').hexdigest()
