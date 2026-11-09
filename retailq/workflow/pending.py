"""Durable order pause inspection; SQL mirrors, never replaces, the checkpoint."""
import math
from pydantic import BaseModel,ConfigDict,Field,model_validator
from retailq.tools.schemas import Identifier
from retailq.errors import ServiceError
from retailq.workflow.contracts import TurnRef
from retailq.workflow.sql import write_sql


class ResumeOrderRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    user_id: Identifier
    interrupt_id: str=Field(min_length=1,max_length=128)
    request_key: str=Field(pattern=r'^[0-9a-f]{32}$')
    order_id: Identifier | None=None
    cancel: bool=False

    @model_validator(mode='after')
    def one_selection(self):
        if (self.order_id is not None)==self.cancel:raise ValueError('Select one order or cancel')
        return self

    def selection(self):
        return {'interrupt_id':self.interrupt_id,**({'cancel':True} if self.cancel else {'order_id':self.order_id})}


def recovery_error():return ServiceError('workflow_recovery_required',409,'暂停状态不一致，请核查当前运行。')


def pending_from_snapshot(snapshot,limit):
    interrupts=snapshot.interrupts
    if not interrupts:return None
    if len(interrupts)!=1:raise recovery_error()
    item=interrupts[0];value=item.value
    if (not isinstance(value,dict) or value.get('kind')!='order_selector'
            or value.get('turn_id')!=snapshot.values.get('turn_id')):raise recovery_error()
    remaining=value.get('pause_budget',{}).get('remaining_active_seconds')
    if type(remaining) not in (float,int) or not math.isfinite(remaining) or not 0<=remaining<=limit:raise recovery_error()
    return {**value,'interrupt_id':item.id}


def public_pending(pending):
    if pending is None:return None
    return {k:pending[k] for k in ('conversation_id','turn_id','interrupt_id','options','message','can_cancel')}


class PendingTurns:
    def __init__(self,repository,runtime):self.repository,self.runtime=repository,runtime

    async def reconcile(self,turn,snapshot):
        pending=pending_from_snapshot(snapshot,self.runtime.settings.workflow_turn_timeout_seconds)
        if pending is not None:
            if pending['conversation_id']!=turn.conversation_id or pending['turn_id']!=turn.turn_id:raise recovery_error()
            await write_sql(self.repository.save_pending,turn,snapshot.values['user_id'],pending,pending['pause_budget']['remaining_active_seconds'])
        return pending

    async def read(self,cid,user_id):
        row=await write_sql(self.repository.latest_turn,cid,user_id)
        snapshot=await self.runtime.snapshot(cid)
        pending=pending_from_snapshot(snapshot,self.runtime.settings.workflow_turn_timeout_seconds)
        if pending:
            if row is None or row['turn_id']!=pending['turn_id']:raise recovery_error()
            turn=TurnRef(row['turn_id'],cid,row['user_message_id'])
            return await self.reconcile(turn,snapshot)
        if row and row['status']=='waiting_order':raise recovery_error()
        return None
