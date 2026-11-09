"""Strict router wire values; never coerce business decisions from malformed JSON."""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import math

from retailq.errors import ServiceError
from retailq.workflow.contracts import strict_object

RouterObserver = Callable[[dict], Awaitable[None]]
INTENTS = ('物流','订单','商品咨询','退款退货','售后','投诉','闲聊','其他')


@dataclass(frozen=True)
class IntentResult:
    intent: str
    confidence: float


@dataclass(frozen=True)
class QueryPlan:
    core_case: bool


def invalid(code):
    return ServiceError(code,502,'模型未返回完整有效的分流信息，请重试。')


def parse_intent(text):
    value=strict_object(text,'intent_output_invalid')
    if (set(value)!={'intent','confidence'} or value['intent'] not in INTENTS
            or type(value['intent']) is not str or type(value['confidence']) not in (int,float)
            or not math.isfinite(value['confidence']) or not 0<=value['confidence']<=1):
        raise invalid('intent_output_invalid')
    return IntentResult(value['intent'],float(value['confidence']))


def parse_resolution(text):
    value=strict_object(text,'resolve_output_invalid')
    question=value.get('resolved_question')
    if set(value)!={'resolved_question'} or type(question) is not str or not question.strip() or len(question)>4000:
        raise invalid('resolve_output_invalid')
    return question


def parse_query_plan(text):
    value=strict_object(text,'query_plan_invalid')
    if set(value)!={'core_case'} or type(value['core_case']) is not bool:
        raise invalid('query_plan_invalid')
    return QueryPlan(value['core_case'])


def parse_queries(text):
    value=strict_object(text,'expand_output_invalid')
    rows=value.get('queries')
    if (set(value)!={'queries'} or not isinstance(rows,list) or not 2<=len(rows)<=4
            or any(type(q) is not str or not q.strip() or len(q)>128 for q in rows)
            or len({q.strip() for q in rows})!=len(rows)):
        raise invalid('expand_output_invalid')
    return tuple(q.strip() for q in rows)
