from pathlib import Path
import pytest
from sqlalchemy import select,func
from langchain_core.messages import AIMessage
from retailq.schemas import ChatRequest
from retailq.models import Message
from retailq.knowledge.rag_models import LowConfidenceQuestion
from retailq.errors import ServiceError

pytestmark = pytest.mark.mysql


@pytest.fixture
def repo(test_database):
    def create():
        from retailq.workflow.repository import WorkflowRepository
        from scripts.migrate_workflow import migrate_workflow
        migrate_workflow(test_database,Path('db/init/05-workflow.sql'))
        return WorkflowRepository(test_database)
    return create


def test_finalization_is_atomic_and_idempotent(repo,test_database):
    repo = repo()
    turn = repo.start_turn(ChatRequest(user_id='alice',message='没有可靠证据的问题'))
    state = {'user_id':'alice','answer':'证据不足','status':'completed','actions':[],
             'gate':{'allowed':False,'reason':'below threshold'},'evidence':None,'agent':{}}
    first = repo.finalize(turn,state)
    assert repo.finalize(turn,state).message_id == first.message_id
    with test_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(LowConfidenceQuestion))==1
        assert session.scalar(select(func.count()).select_from(Message).where(
            Message.conversation_id==turn.conversation_id,Message.role=='assistant'))==1
        pool = session.scalar(select(LowConfidenceQuestion))
        assert pool.raw_question=='没有可靠证据的问题' and pool.source=='retrieval_low_conf'
    with pytest.raises(ServiceError) as error: repo.finalize(turn,{**state,'answer':'changed'})
    assert error.value.code=='workflow_result_conflict'
    repo.mark_terminal(turn,'failed','checkpoint_failed')
    assert repo.get_turn(turn.turn_id,'alice')['status']=='completed'


def test_finalization_failure_rolls_back_assistant_and_pool(repo,test_database,monkeypatch):
    repo = repo()
    turn = repo.start_turn(ChatRequest(user_id='alice',message='原始问题'))
    def fail(*args,**kwargs): raise RuntimeError('fail after assistant flush')
    monkeypatch.setattr(repo,'_create_actions',fail)
    with pytest.raises(RuntimeError):
        repo.finalize(turn,{'user_id':'alice','answer':'answer','status':'completed','actions':[],
                           'gate':{'allowed':False,'reason':'weak'}})
    with test_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Message).where(
            Message.conversation_id==turn.conversation_id,Message.role=='assistant'))==0
        assert session.scalar(select(func.count()).select_from(LowConfidenceQuestion))==0


def test_owned_history_excludes_current_user_and_preserves_prior_turn(repo):
    repo = repo()
    one = repo.start_turn(ChatRequest(user_id='alice',message='上一轮'))
    repo.finalize(one,{'user_id':'alice','answer':'已答','status':'completed','actions':[]})
    two = repo.start_turn(ChatRequest(user_id='alice',message='本轮',conversation_id=one.conversation_id))
    turns = repo.load_projection(one.conversation_id,'alice',before_message_id=two.user_message_id)
    assert [m.content for t in turns for m in t] == ['上一轮','已答']
    with pytest.raises(ServiceError) as error: repo.load_projection(one.conversation_id,'bob')
    assert error.value.status_code==404


def test_tool_messages_are_idempotent_and_case_sensitive(repo):
    repo = repo()
    turn = repo.start_turn(ChatRequest(user_id='alice',message='订单1001'))
    def selection(cid): return AIMessage(content='',tool_calls=[{'id':cid,'name':'query_order','args':{'order_id':'1001'}}])
    first = repo.record_tool_request(turn,selection('call'))
    assert repo.record_tool_request(turn,selection('call'))==first
    receipt = repo.record_tool_result(turn,'call',{'ok':True,'data':{'order_id':'1001'}})
    assert repo.record_tool_result(turn,'call',{'ok':True,'data':{'order_id':'1001'}})==receipt
    assert repo.record_tool_request(turn,selection('CALL')) != first
    with pytest.raises(ServiceError): repo.record_tool_result(turn,'call',{'ok':False})


def test_action_suggestions_are_bound_to_final_message(repo):
    repo = repo()
    turn = repo.start_turn(ChatRequest(user_id='alice',message='我要投诉'))
    final = repo.finalize(turn,{'user_id':'alice','answer':'请选择','status':'completed','actions':['handoff','ticket']})
    actions = repo.list_actions(turn.conversation_id,'alice',final.message_id)
    assert {a['kind'] for a in actions}=={'handoff','ticket'}
    assert all(a['origin_assistant_message_id']==str(final.message_id) for a in actions)
    assert len(repo.finalize(turn,{'user_id':'alice','answer':'请选择','status':'completed','actions':['handoff','ticket']}).actions)==2
    with pytest.raises(ServiceError): repo.list_actions(turn.conversation_id,'bob',final.message_id)


def test_old_button_fact_is_projected_once_with_its_original_source(repo,test_database):
    import json
    from retailq.repositories import ChatRepository
    from retailq.workflow.models import WorkflowAction
    repo=repo()
    first=repo.start_turn(ChatRequest(user_id='alice',message='我要投诉'))
    final=repo.finalize(first,{'user_id':'alice','answer':'请选择','status':'completed','actions':['ticket']})
    second=repo.start_turn(ChatRequest(user_id='alice',conversation_id=first.conversation_id,message='先继续聊别的问题'))
    repo.finalize(second,{'user_id':'alice','answer':'可以继续','status':'completed','actions':[]})
    action=final.actions[0]
    with test_database.sessions() as session: call_id=session.get(WorkflowAction,action['action_id']).tool_call_id
    chat=ChatRepository(test_database)
    request_id=chat.append_message(first.conversation_id,'alice','assistant',None,[{'id':call_id,'type':'function',
        'source':'user_action','origin_assistant_message_id':str(final.message_id),
        'function':{'name':'create_ticket','arguments':'{"description":"旧问题","ticket_type":"投诉"}'}}])
    result={'ticket_no':'T-CONTEXT-1','status':'待处理'}
    chat.append_message(first.conversation_id,'alice','tool',json.dumps({'ok':True,'data':result}),None,call_id)
    with test_database.sessions.begin() as session:
        row=session.get(WorkflowAction,action['action_id']);row.request_message_id=request_id
        row.status='completed';row.result=result
    third=repo.start_turn(ChatRequest(user_id='alice',conversation_id=first.conversation_id,message='工单建好了吗'))
    projection=repo.load_projection(first.conversation_id,'alice',before_message_id=third.user_message_id)
    all_messages=[m for t in projection for m in t]
    facts=[m for m in all_messages if 'T-CONTEXT-1' in m.content]
    assert len(facts)==1 and f'原回复{final.message_id}' in facts[0].content
    assert not any(m.type=='ai' and m.tool_calls for m in all_messages)


def test_evidence_snapshot_is_immutable_and_authorized(repo,test_database):
    from copy import deepcopy
    from retailq.workflow.models import WorkflowTurn
    from dataclasses import asdict
    from retailq.knowledge.answer import prepare_bundle
    from tests.test_rag_answer import retrieval
    repo=repo(); turn=repo.start_turn(ChatRequest(user_id='alice',message='运费多少'))
    bundle=prepare_bundle(retrieval(1),system_prompt='依据证据回答',input_budget=6000)
    evidence={'citations':[asdict(c) for c in bundle.citations],'bundle_hash':bundle.bundle_hash}
    final=repo.finalize(turn,{'user_id':'alice','answer':'见[1]','status':'completed','actions':[],'evidence':evidence})
    assert len(repo.load_citations(turn.conversation_id,'alice',final.evidence_id))==1
    with pytest.raises(ServiceError): repo.load_citations(turn.conversation_id,'bob',final.evidence_id)
    with test_database.sessions.begin() as session:
        row=session.get(WorkflowTurn,turn.turn_id); payload=deepcopy(row.payload)
        payload['final']['evidence']['citations'][0]['text']='tampered';row.payload=payload
    with pytest.raises(ServiceError) as error: repo.load_citations(turn.conversation_id,'alice',final.evidence_id)
    assert error.value.code=='citation_invalid'
