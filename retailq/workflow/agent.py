"""An explicit tool loop. No LangGraph imports: the graph reuses its steps."""
import asyncio
from contextlib import aclosing
import json
import time

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from retailq.context import estimate_tokens
from retailq.errors import ServiceError
from retailq.services.chat import ChatEvent
from retailq.workflow.budget import BudgetExceeded, BudgetLedger
from retailq.workflow.sql import write_sql

READ_TOOLS = frozenset(('query_order', 'query_product', 'query_logistics'))
LIMIT_ANSWER = '本轮查询已达到步骤或预算上限，请补充信息或分开提问；已完成的查询记录会保留。'
DECISION_STAGE = '[workflow_decision] 当前请求仅为控制决策阶段，不是最终答复阶段。每次最多申请一个工具、查询一个订单。即使用户要求查多单，也必须先查一单、等待真实回执，再在下一次决策查另一单；禁止一次输出多个tool_calls。若无需调用工具，只输出单个JSON对象，例如 {"actions":[]}。禁止在JSON前后输出任何自然语言、追问或解释；下一次独立请求才生成用户可见的答复。'


def answer_instruction(actions=(),knowledge=False,refund=False):
    instruction='现在根据用户问题、证据和工具结果直接给出最终答复；不要输出动作JSON或工具申请。'
    labels=[{'handoff':'转人工','ticket':'申请退款' if refund else '建工单'}[kind] for kind in actions]
    instruction+='本轮实际可用按钮：'+json.dumps(labels,ensure_ascii=False,separators=(',',':'))+'。只能提及本轮实际存在的中文按钮名，不输出内部动作标识；空列表时不得说下方有按钮。'
    instruction+='缺少业务ID时只追问现有工具对应的ID，物流只支持订单号，不要求快递单号，不索要昵称或手机号，不建议本系统不具备的查询方式。'
    if actions:instruction+='动作只由用户点击回复下方的独立按钮并确认执行；请说明点击按钮，不要求回复数字或文字触发动作。'
    if knowledge:instruction+='本轮有检索资料，答复必须含对应[n]引用；即使资料未说明所问事实，也引用已查阅的相关资料来界定范围，不捏造事实。'
    return instruction


def stage_reserve():
    return max(estimate_tokens([SystemMessage(content=DECISION_STAGE)]),
        estimate_tokens([SystemMessage(content=answer_instruction(('handoff','ticket'),True))]))


class AgentSteps:
    def __init__(self, gateway, registry, settings, emit, *, repository=None, turn=None, deadline=None):
        self.gateway, self.registry, self.settings, self.emit = gateway, registry, settings, emit
        self.repository, self.turn = repository, turn
        self.deadline = deadline if deadline is not None else time.monotonic() + settings.workflow_turn_timeout_seconds
        self.tools = [t for t in registry.tools if t.name in READ_TOOLS]
        self.schema_cost = len(json.dumps([convert_to_openai_tool(t) for t in self.tools],ensure_ascii=False).encode('utf-8'))

    def _agent(self, state):
        return {**{'step':0, 'model_calls':0, 'call_ids':[], 'seen_calls':[], 'pending_selection':None,
                   'usage':{'entries':[]}}, **state.get('agent',{})}

    def _reserve(self, state, *, tools, stage):
        agent = self._agent(state)
        if agent['model_calls'] >= self.settings.workflow_max_model_calls or time.monotonic() >= self.deadline:
            raise BudgetExceeded()
        cost = estimate_tokens(state['messages']) + (self.schema_cost if tools else 0)
        if cost > self.settings.input_budget: raise BudgetExceeded()
        ledger = BudgetLedger(self.settings.workflow_max_total_tokens,agent['usage'])
        cap = ledger.remaining_output(cost,self.settings.max_output_tokens)
        if cap < 1: raise BudgetExceeded()
        reservation = ledger.reserve(cost,cap,stage=stage)
        agent['model_calls'] += 1
        agent['usage'] = ledger.snapshot()
        return agent, ledger, reservation, cap

    def limited(self, state, *, repeated=False):
        answer = '检测到重复查询，已停止本轮循环，请补充信息或选择人工协助。' if repeated else LIMIT_ANSWER
        self.emit(ChatEvent('token',{'text':answer}))
        return {'agent':self._agent(state), 'status':'limited', 'answer':answer, 'finish_reason':'stop', 'actions':[]}

    async def decide(self, state):
        agent = self._agent(state)
        if agent['step'] >= self.settings.workflow_max_tool_steps:
            return self.limited(state)
        decision_state={**state,'messages':[*state['messages'],SystemMessage(content=DECISION_STAGE)]}
        restricted=state.get('route') in ('core','other')
        try: agent, ledger, reservation, cap = self._reserve(decision_state, tools=not restricted, stage='agent_decision')
        except BudgetExceeded: return self.limited(state)
        async with asyncio.timeout(max(0,self.deadline-time.monotonic())):
            decision = await self.gateway.decide_agent(decision_state['messages'],[] if restricted else self.tools,max_tokens=cap)
        ledger.settle(reservation, decision.charge)
        agent['usage'] = ledger.snapshot()
        calls = decision.selection.tool_calls
        if len(calls) > 1: raise ServiceError('multiple_tool_calls',502,'单步只能执行一个工具，本轮未执行工具。')
        if calls:
            call = calls[0]
            if restricted or call['name'] not in READ_TOOLS:
                raise ServiceError('agent_tool_denied',502,'该工具不能由客服 Agent 自动执行。')
            if call['id'] in agent['call_ids']:
                raise ServiceError('reused_tool_call_id',502,'模型重复使用工具申请，未执行。')
            signature = json.dumps([call['name'],call['args']],sort_keys=True,ensure_ascii=False)
            if signature in agent['seen_calls']:
                return self.limited({**state,'agent':agent},repeated=True)
            agent['seen_calls'] = [*agent['seen_calls'],signature]
            agent['call_ids'] = [*agent['call_ids'],call['id']]
            agent['pending_selection'] = decision.selection.model_dump()
            return {'agent':agent, 'status':'tool'}
        agent['pending_selection'] = None
        return {'agent':agent, 'actions':list(decision.actions), 'status':'answer'}

    async def execute(self, state):
        if time.monotonic()>=self.deadline:return self.limited(state)
        agent = self._agent(state)
        selection = AIMessage.model_validate(agent['pending_selection'])
        call = selection.tool_calls[0]
        if state.get('route') in ('core','other') or call['name'] not in READ_TOOLS: raise ServiceError('agent_tool_denied',502,'禁止自动执行该工具。')
        if self.repository is not None:
            await write_sql(self.repository.record_tool_request,self.turn,selection)
        self.registry.context.tool_call_id = call['id']
        status = {'tool_call_id':call['id'],'name':call['name']}
        self.emit(ChatEvent('tool_status',{**status,'status':'running'}))
        if time.monotonic()>=self.deadline:return self.limited(state)
        try:
            async with asyncio.timeout(max(0,self.deadline-time.monotonic())):
                result = await self.registry.execute(call)
        except TimeoutError:return self.limited(state)
        if self.repository is not None:
            await write_sql(self.repository.record_tool_result,self.turn,call['id'],result.payload)
        content = json.dumps(result.payload,ensure_ascii=False)
        message = ToolMessage(content=content,tool_call_id=call['id'],name=call['name'])
        self.emit(ChatEvent('tool_status',{**status,'status':'completed' if result.payload['ok'] else 'failed'}))
        agent['step'] += 1
        agent['pending_selection'] = None
        return {'agent':agent,'messages':[*state['messages'],selection,message], 'status':'decide'}

    async def answer(self, state):
        # The control JSON never becomes assistant prose or model history.
        instruction=answer_instruction(state.get('actions',[]),bool(state.get('evidence',{}).get('citations')),state.get('route')=='core' and state.get('intent')=='退款退货')
        if state.get('route')=='core':instruction+='订单和政策已由流程确认，只判断本单条件；未知商品状态或缺材料应条件化说明或必要澄清。商品名称不能证明非定制或包装完好；是否定制、是否影响二次销售等未知字段必须保留条件，不可默认满足。不要追问订单号、退款原因，不把未知字段当事实，不承诺已退款。'
        if state.get('route')=='other':instruction+='当前诉求不明确或超出购物客服范围，只作简短澄清或范围说明，不声称已查询业务。'
        final_messages = [*state['messages'],SystemMessage(content=instruction)]
        working = {**state,'messages':final_messages}
        try: agent, ledger, reservation, cap = self._reserve(working,tools=False,stage='agent_answer')
        except BudgetExceeded: return self.limited(state)
        text, finish, usage = [], None, None
        async with asyncio.timeout(max(0,self.deadline-time.monotonic())):
            async with aclosing(self.gateway.stream_answer(final_messages,max_tokens=cap)) as stream:
                async for chunk in stream:
                    if chunk.text:
                        text.append(chunk.text)
                        self.emit(ChatEvent('token',{'text':chunk.text}))
                    if chunk.finish_reason: finish = chunk.finish_reason
                    if chunk.usage is not None: usage = chunk.usage
        ledger.settle(reservation,usage)
        agent['usage'] = ledger.snapshot()
        if not text or finish not in ('stop','length'):
            raise ServiceError('incomplete_upstream_stream',502,'模型回复未完整结束，请重试。')
        return {'agent':agent,'answer':''.join(text),'finish_reason':finish,
                'status':'completed' if finish=='stop' else 'interrupted',
                'actions':state.get('actions',[]) if finish=='stop' else []}


async def run_bare_agent(state, steps):
    state = {**state,'status':'decide'}
    while state['status'] in ('decide','tool','answer'):
        method = {'decide':steps.decide,'tool':steps.execute,'answer':steps.answer}[state['status']]
        state = {**state, **await method(state)}
    return state
