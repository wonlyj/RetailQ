from dataclasses import dataclass
import json
from typing import Literal, TypedDict

from langchain_core.messages import AIMessage
from retailq.errors import ServiceError


@dataclass(frozen=True)
class UsageCharge:
    input_tokens: int
    output_tokens: int
    source: Literal['provider', 'estimate', 'reserved']


@dataclass(frozen=True)
class AgentDecision:
    selection: AIMessage
    actions: tuple[str, ...]
    charge: UsageCharge | None = None


@dataclass(frozen=True)
class TurnRef:
    turn_id: str
    conversation_id: int
    user_message_id: int


@dataclass(frozen=True)
class FinalizedTurn:
    turn_id: str
    message_id: int
    evidence_id: str | None
    actions: tuple[dict, ...]


@dataclass(frozen=True)
class TicketActionResult:
    action_id: str
    status: str
    ticket: dict | None


class WorkflowState(TypedDict, total=False):
    turn_id: str
    user_message_id: int
    conversation_id: int
    user_id: str
    raw_question: str
    resolved_question: str
    messages: list
    intent: str
    route: str
    evidence: dict | None
    gate: dict | None
    agent: dict
    answer: str
    finish_reason: str | None
    actions: list
    final_message_id: int | None
    trace: list
    status: str


def strict_object(text: str, code: str) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=unique)
        if not isinstance(value, dict): raise ValueError('object required')
        return value
    except (ValueError, TypeError):
        raise ServiceError(code, 502, '模型未返回有效的工作流控制信息，请重试。') from None


def parse_actions(text: str) -> tuple[str, ...]:
    value = strict_object(text, 'agent_control_invalid')
    actions = value.get('actions')
    if (set(value) != {'actions'} or not isinstance(actions, list) or len(actions) > 2
            or any(type(a) is not str or a not in ('handoff', 'ticket') for a in actions)
            or len(set(actions)) != len(actions)):
        raise ServiceError('agent_control_invalid', 502, '模型的可选动作格式无效，请重试。')
    return tuple(actions)


def usage_charge(metadata) -> UsageCharge | None:
    if not isinstance(metadata, dict): return None
    i, o = metadata.get('input_tokens'), metadata.get('output_tokens')
    if type(i) is not int or type(o) is not int or min(i, o) < 0: return None
    return UsageCharge(i, o, 'provider')
