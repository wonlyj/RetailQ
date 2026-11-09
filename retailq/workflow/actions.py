"""User-confirmed ticket writes; no agent and no handoff backend action."""
import asyncio
from functools import partial
import logging
import anyio

from retailq.errors import ServiceError
from retailq.tools.schemas import TicketInput
from retailq.tools.runtime import ToolContext,build_registry
from retailq.workflow.sql import write_sql

logger=logging.getLogger('retailq.workflow')


class TicketActionService:
    def __init__(self,repository,chat_repository,coordinator,settings):
        self.repository,self.chat_repository,self.coordinator,self.settings=repository,chat_repository,coordinator,settings
        self.background=set();self.closing=False

    async def _sql(self,method,*args):
        return await write_sql(method,*args)

    def _background(self,registry,lease,cid,user_id,action_id):
        async def finish():
            try:
                await registry.drain()
                await self._sql(self.repository.reconcile_action,cid,user_id,action_id)
            except Exception:
                logger.error('ticket_settlement_unconfirmed action_id=%s',action_id)
            finally:lease.close()
        task=asyncio.create_task(finish());self.background.add(task);task.add_done_callback(self.background.discard)

    async def confirm(self,cid,user_id,action_id,description,ticket_type):
        return await self._confirm(cid,user_id,action_id,description,ticket_type)

    async def confirm_refund(self,cid,user_id,action_id,reason,request_key):
        from retailq.workflow.refunds import RefundRequest,refund_description
        body=RefundRequest(user_id=user_id,reason=reason,request_key=request_key)
        binding=await asyncio.to_thread(self.repository.action_binding,cid,user_id,action_id)
        if not binding or binding.get('subtype')!='refund':raise ServiceError('refund_action_required',409,'当前回复没有绑定的退款申请。')
        refund={'order_id':binding['order_id'],'reason':body.reason,'request_key':body.request_key}
        return await self._confirm(cid,user_id,action_id,refund_description(binding['order_id'],body.reason),'退款',refund=refund)

    async def _confirm(self,cid,user_id,action_id,description,ticket_type,*,refund=None):
        if self.closing:raise ServiceError('workflow_closing',503,'工单服务正在关闭。')
        args=TicketInput.model_validate({'description':description,'ticket_type':ticket_type}).model_dump()
        # Ownership and suggestion eligibility are established before taking a lease.
        await asyncio.to_thread(self.repository.read_action,cid,user_id,action_id)
        lease=self.coordinator.try_acquire('conversation',str(cid))
        if lease is None:raise ServiceError('conversation_busy',409,'该会话仍在处理请求，请稍后核查。')
        registry=None;transferred=False
        try:
            if refund is None:
                claim=await self._sql(self.repository.claim_action,cid,user_id,action_id,args['description'],args['ticket_type'])
            else:
                claim=await write_sql(self.repository.claim_action,cid,user_id,action_id,args['description'],args['ticket_type'],refund=refund)
            if not claim['is_new']:
                return await self._sql(self.repository.reconcile_action,cid,user_id,action_id)
            registry=build_registry(self.chat_repository,ToolContext(cid,user_id,claim['tool_call_id'],args['description']),self.settings)
            outcome=await registry.execute({'id':claim['tool_call_id'],'name':'create_ticket','args':args})
            result=await self._sql(self.repository.settle_action,cid,user_id,action_id,outcome)
            if registry.pending:
                self._background(registry,lease,cid,user_id,action_id);transferred=True
            return result
        except BaseException:
            if registry is not None and registry.pending:
                self._background(registry,lease,cid,user_id,action_id);transferred=True
            raise
        finally:
            if not transferred:lease.close()

    async def get(self,cid,user_id,action_id):
        result=await asyncio.to_thread(self.repository.read_action,cid,user_id,action_id)
        if result.status in ('offered','completed'):return result
        lease=self.coordinator.try_acquire('conversation',str(cid))
        if lease is None:return result
        try:return await self._sql(self.repository.reconcile_action,cid,user_id,action_id)
        finally:lease.close()

    async def aclose(self):
        self.closing=True
        if self.background:await asyncio.gather(*list(self.background),return_exceptions=True)
