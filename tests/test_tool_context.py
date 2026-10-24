from datetime import datetime
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from retailq.config import Settings
from retailq.errors import ServiceError
from retailq.repositories import MessageRecord


def record(id,role,content=None,calls=None,call_id=None):
    return MessageRecord(id,1,role,content,calls,call_id,datetime.now())


def test_interrupted_and_late_tool_receipts_restore_original_turn():
    from retailq.context import history_turns
    calls = [{'id':'c1','type':'function','function':{'name':'create_ticket','arguments':'{"description":"迟到","ticket_type":"投诉"}'}}]
    rows = [record(1,'user','请投诉'),record(2,'assistant',calls=calls),
            record(3,'assistant','工单结果尚未确认。'),record(4,'user','新问题'),
            record(5,'tool','{"ok":true,"data":{"ticket_no":"T1"}}',call_id='c1')]
    turns = history_turns(rows)
    assert len(turns) == 1
    assert [m.type for m in turns[0]] == ['human','ai','tool','ai']
    assert turns[0][2].tool_call_id == 'c1'
    assert 'T1' in turns[0][2].content
    assert history_turns(rows[:2]) == []
    interrupted = history_turns([rows[0],rows[1],rows[4]])
    assert [m.type for m in interrupted[0]] == ['human','ai','tool']


def test_schema_and_argument_cost_and_whole_turn_trimming():
    from retailq.context import prepare_tool_chat
    settings = Settings(_env_file=None,context_window_tokens=6000,max_output_tokens=200,token_safety_margin=100)
    turn = [HumanMessage('旧订单'*100),AIMessage('旧回答'*100)]
    schema = [{'type':'function','function':{'name':'demo','description':'说明'*150}}]
    prepared = prepare_tool_chat('订单1001物流',[turn,turn],schema,settings)
    assert prepared.input_tokens_estimate <= settings.input_budget
    assert prepared.dropped_history_messages % 2 == 0
    with pytest.raises(ServiceError):
        prepare_tool_chat('问题',[],[{'description':'字'*2000}],settings)


def test_tool_answer_over_budget_and_no_tool_second_stage():
    from retailq.context import prepare_tool_chat, prepare_tool_answer
    settings = Settings(_env_file=None)
    prepared = prepare_tool_chat('订单1001物流',[],[],settings)
    selection = AIMessage('',tool_calls=[{'id':'c1','name':'query_logistics','args':{'order_id':'1001'}}])
    with pytest.raises(ServiceError):
        prepare_tool_answer(prepared,selection,ToolMessage('字'*3000,tool_call_id='c1'),settings)
    final = prepare_tool_answer(prepared,AIMessage('第一阶段不是流式答案'),None,settings)
    assert final == prepared.messages
