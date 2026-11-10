import json
import re
import time
from langgraph.types import interrupt
from langchain_core.messages import AIMessage, ToolMessage
from retailq.errors import ServiceError
from retailq.services.chat import ChatEvent
from retailq.workflow.sql import write_sql

_ORDER=re.compile(r'订单(?:号)?\s*[:：]?\s*([A-Za-z0-9_-]+(?:\s*(?:和|与|及|、|，|,)\s*[A-Za-z0-9_-]+)*)')
_CHANGED_TARGET=re.compile(
    r'另(?:一|个|笔|件)|其他|别的|换(?:一|个|笔|件)|新(?:的)?(?:订单|商品)|'
    r'(?:不是|不要|别选|不选|排除|不退|不处理).{0,16}(?:订单|这单|那单|这笔|那笔|刚才|之前)|'
    r'订单.{0,24}(?:不退|不选|不要)|(?:这|那|该)(?:笔|个)?单.{0,16}(?:不退|不选|不要)')
_HISTORY_POINTER=re.compile(r'它|这个|那个|这单|那单|该单|这笔|那笔|刚才|刚刚|上面')


def _explicit(text):
    return tuple(dict.fromkeys(value for group in _ORDER.findall(text) for value in re.split(r'\s*(?:和|与|及|、|，|,)\s*',group)))


def confirmed_order_ids(messages,raw_question):
    # A trusted mention is not necessarily a selected target. Ambiguity and
    # exclusions use the existing picker; model rewrites never authorize IDs.
    if _CHANGED_TARGET.search(raw_question):return ()
    current=_explicit(raw_question)
    if current:return current
    if (not _HISTORY_POINTER.search(raw_question)
            or re.search(r'商品(?:编号|号)?\s*[:：]?\s*[A-Za-z0-9_-]+',raw_question)):
        return ()
    ids=[]
    last_human=next((i for i in range(len(messages)-1,-1,-1) if messages[i].type=='human'),None)
    calls={}
    for i,message in enumerate(messages):
        if message.type=='human' and i!=last_human:ids.extend(_explicit(message.content))
        if message.type=='ai':
            for c in message.tool_calls:
                if c['name'] in ('query_order','query_logistics'):calls[c['id']]=c
        if message.type=='tool' and message.tool_call_id in calls:
            try:
                value=json.loads(message.content)
                if value.get('ok') and type(value.get('data',{}).get('order_id')) is str:
                    ids.append(value['data']['order_id'])
            except (ValueError,TypeError,AttributeError):pass
    return tuple(dict.fromkeys(ids))


class OrderSteps:
    def __init__(self,provider,registry,repository,turn,*,deadline=None,emit=None):
        self.provider,self.registry,self.repository,self.turn=provider,registry,repository,turn
        self.deadline,self.emit=deadline,emit

    async def resolve_order(self,state):
        ids=confirmed_order_ids(state['messages'],state['raw_question'])
        order_id=ids[0] if len(ids)==1 else None
        if order_id:self.provider.get_order(state['user_id'],order_id)
        return {'order_id':order_id,'order_options':list(self.provider.list_orders(state['user_id'])) if order_id is None else []}

    async def load_order(self,state):
        if self.deadline is not None and time.monotonic()>=self.deadline:
            raise ServiceError('workflow_timeout',504,'本轮订单查询已超时。')
        agent={**state.get('agent',{})}
        if agent.get('step',0)>=self.registry.settings.workflow_max_tool_steps:
            raise ServiceError('workflow_budget_exceeded',422,'本轮订单查询步骤不足。')
        call_id='ord_'+self.turn.turn_id
        call={'id':call_id,'name':'query_order','args':{'order_id':state['order_id']}}
        selection=AIMessage(content='',tool_calls=[call])
        payload=await write_sql(self.repository.load_tool_outcome,self.turn,call_id)
        if payload is None:
            await write_sql(self.repository.record_tool_request,self.turn,selection)
            self.registry.context.tool_call_id=call_id
            if self.emit:self.emit(ChatEvent('tool_status',{'tool_call_id':call_id,'name':'query_order','status':'running'}))
            outcome=await self.registry.execute(call)
            payload=outcome.payload
            await write_sql(self.repository.record_tool_result,self.turn,call_id,payload)
        if self.emit:self.emit(ChatEvent('tool_status',{'tool_call_id':call_id,'name':'query_order','status':'completed' if payload.get('ok') else 'failed'}))
        if not payload.get('ok'):raise ServiceError('order_lookup_failed',502,'订单查询失败，本轮未继续判断。')
        agent['step']=agent.get('step',0)+1
        return {'order_snapshot':payload['data'],'agent':agent,
                'messages':[*state['messages'],selection,ToolMessage(content=json.dumps(payload,ensure_ascii=False),tool_call_id=call_id,name='query_order')]}

    async def select_order(self,state):
        remaining=max(0,self.deadline-time.monotonic()) if self.deadline is not None else state.get('remaining_active_seconds',120)
        selection=interrupt({'kind':'order_selector','conversation_id':state['conversation_id'],'turn_id':state['turn_id'],
            'options':state['order_options'],'message':'请选择需要办理退款或售后的订单。','can_cancel':True,
            'pause_budget':{'remaining_active_seconds':remaining}})
        if not isinstance(selection,dict):raise ServiceError('order_selection_invalid',422,'请选择有效订单。')
        if selection.get('cancel') is True:return {'status':'cancelled','order_selection':selection,'actions':[]}
        order_id=selection.get('order_id')
        if order_id not in {row['order_id'] for row in state['order_options']}:
            raise ServiceError('order_selection_invalid',422,'请选择当前卡片中的订单。')
        self.provider.get_order(state['user_id'],order_id)
        return {'order_id':order_id,'order_selection':selection,'status':'active'}
