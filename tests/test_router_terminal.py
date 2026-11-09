from types import SimpleNamespace
import time
import pytest
from retailq.config import Settings
from retailq.workflow.contracts import TurnRef
from retailq.workflow.service import WorkflowChatService,PreparedWorkflowTurn


@pytest.mark.anyio
@pytest.mark.parametrize('terminal',['done','paused','cancelled'])
async def test_terminal_is_published_after_request_resources_release(terminal):
    state={'status':'cancelled' if terminal=='cancelled' else 'completed','final_message_id':99,'answer':'答复','actions':[]}
    async def snapshot(config):return SimpleNamespace(values=state,next=() if terminal!='paused' else ('select_order',))
    runtime=SimpleNamespace(settings=Settings(_env_file=None),gateway=None,chat_repository=None,coordinator=None,
        repository=SimpleNamespace(list_actions=lambda *a:[]),config_for=lambda cid:{})
    service=WorkflowChatService(runtime)
    async def events(*args):
        if False:yield None
    service._graph_events=events
    pending={'conversation_id':1,'turn_id':'a'*32,'interrupt_id':'intr','options':[],'message':'请选择','can_cancel':True}
    async def reconcile(*args):return pending if terminal=='paused' else None
    service.pending_turns.reconcile=reconcile
    released=[]
    async def release(turn):released.append(True)
    service.release=release
    turn=PreparedWorkflowTurn(TurnRef('a'*32,1,1),'alice','问题',
        SimpleNamespace(input_tokens_estimate=0,dropped_history_messages=0),None,None,time.monotonic()+10,
        graph=SimpleNamespace(aget_state=snapshot))
    async for event in service.stream(turn):
        if event.event==terminal:assert released==[True]
