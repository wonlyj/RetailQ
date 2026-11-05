from pathlib import Path

from langchain_core.messages import SystemMessage
from retailq.context import estimate_tokens, require_budget
from retailq.errors import ServiceError
from retailq.workflow.budget import BudgetExceeded, BudgetLedger
from retailq.workflow.contracts import strict_object

ROUTES = {'物流':'business','订单':'business','售后':'business','商品咨询':'knowledge',
          '退款退货':'knowledge','投诉':'complaint','闲聊':'smalltalk'}
COMPLAINT_REPLY = '很抱歉给您带来不好的体验，您可以选择转人工客服，或创建工单记录问题。'
SMALLTALK_REPLY = '您好，我是 RetailQ 购物助手，可以帮您咨询商品、订单、物流及售后问题。'
LOW_CONFIDENCE_REPLY = '知识库没有足够可靠的依据，暂时无法回答这个问题。您可以补充说明或查看官方规则。'


def resolve_reference(question: str) -> str:
    return question


def parse_intent(text: str) -> str:
    result = strict_object(text,'intent_output_invalid')
    if set(result) != {'intent'} or type(result['intent']) is not str or result['intent'] not in ROUTES:
        raise ServiceError('intent_output_invalid',502,'模型未返回七类意图之一，请重试。')
    return result['intent']


def route_intent(intent: str) -> str:
    if intent not in ROUTES: raise ServiceError('intent_output_invalid',502,'无法分流未知意图。')
    return ROUTES[intent]


class IntentClassifier:
    def __init__(self,gateway,settings):
        self.gateway,self.settings = gateway,settings
        self.prompt = (Path(__file__).resolve().parents[1]/'prompts/workflow_intent.txt').read_text(encoding='utf-8')

    async def classify(self,state):
        history = [m for m in state['messages'] if m.type!='system']
        messages = [SystemMessage(content=self.prompt),*history]
        cost = estimate_tokens(messages)
        require_budget(cost,self.settings.input_budget)
        agent = {**state.get('agent',{})}
        ledger = BudgetLedger(self.settings.workflow_max_total_tokens,agent.get('usage'))
        try: reservation = ledger.reserve(cost,256,stage='intent')
        except BudgetExceeded:
            raise ServiceError('workflow_budget_exceeded',422,'本轮模型预算不足，请缩短问题。') from None
        intent,charge = await self.gateway.classify_intent(messages,max_tokens=256)
        ledger.settle(reservation,charge)
        agent['usage'] = ledger.snapshot()
        return {'intent':intent,'route':route_intent(intent),'agent':agent}
