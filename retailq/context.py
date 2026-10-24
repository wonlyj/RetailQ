from dataclasses import dataclass
import json

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, trim_messages

from retailq.config import Settings
from retailq.errors import ServiceError
from retailq.prompts import build_chat_messages, build_extract_messages, load_system_prompt, schema_json
from retailq.schemas import ChatRequest


@dataclass(frozen=True)
class PreparedChat:
    messages: list[BaseMessage]
    input_tokens_estimate: int
    dropped_history_messages: int


def estimate_tokens(messages: list[BaseMessage]) -> int:
    """Conservative UTF-8 estimate, not a provider-specific tokenizer count."""
    return sum(len(message.content.encode("utf-8")) + 8
               + (len(json.dumps(message.tool_calls, ensure_ascii=False).encode('utf-8')) if isinstance(message,AIMessage) and message.tool_calls else 0)
               + (len(message.tool_call_id.encode('utf-8')) if isinstance(message,ToolMessage) else 0)
               for message in messages)


def require_budget(estimate: int, budget: int) -> None:
    if estimate > budget:
        raise ServiceError("input_budget_exceeded", 422, "输入超出模型预算，请缩短当前问题或描述。")


def prepare_chat(request: ChatRequest, settings: Settings) -> PreparedChat:
    messages = build_chat_messages(request, load_system_prompt())
    fixed = [messages[0], messages[-1]]
    fixed_cost = estimate_tokens(fixed)
    require_budget(fixed_cost, settings.input_budget)
    history = trim_messages(
        messages[1:-1], max_tokens=settings.input_budget - fixed_cost, token_counter=estimate_tokens,
        strategy="last", allow_partial=False, start_on="human", end_on="ai",
    )
    # Trimming can leave a final user if a preceding assistant alone exceeded budget.
    # start_on/end_on enforce roles; original validated alternating history preserves pairs.
    prepared = [messages[0], *history, messages[-1]]
    return PreparedChat(prepared, estimate_tokens(prepared), len(request.history) - len(history))


def prepare_extraction(description: str, settings: Settings) -> list[BaseMessage]:
    messages = build_extract_messages(description)
    estimate = estimate_tokens(messages) + len(schema_json().encode("utf-8")) + 32
    require_budget(estimate, settings.input_budget)
    return messages


def history_turns(records) -> list[list[BaseMessage]]:
    """Recover complete role graphs, including late receipts by call ID."""
    results = {r.tool_call_id:r for r in records if r.role == 'tool' and r.tool_call_id}
    groups, current = [], []
    for row in records:
        if row.role == 'user':
            if current:
                groups.append(current)
            current = [row]
        elif current and row.role != 'tool':
            current.append(row)
    if current:
        groups.append(current)
    turns = []
    for group in groups:
        turn = [HumanMessage(content=group[0].content or '')]
        valid, has_answer = True, False
        for row in group[1:]:
            if row.tool_calls:
                calls = []
                try:
                    for raw in row.tool_calls:
                        args = json.loads(raw['function']['arguments'])
                        if not isinstance(args,dict) or raw['id'] not in results:
                            raise ValueError('incomplete graph')
                        calls.append({'id':raw['id'],'name':raw['function']['name'],'args':args})
                except (KeyError,ValueError,TypeError):
                    valid = False
                    break
                turn.append(AIMessage(content=row.content or '',tool_calls=calls))
                for call in calls:
                    result = results[call['id']]
                    turn.append(ToolMessage(content=result.content or '',tool_call_id=call['id'],name=call['name']))
                has_answer = True
            elif row.content:
                turn.append(AIMessage(content=row.content))
                has_answer = True
        if valid and has_answer:
            turns.append(turn)
    return turns


def prepare_tool_chat(message: str, turns: list[list[BaseMessage]], tool_schemas: list[dict], settings: Settings) -> PreparedChat:
    system, user = SystemMessage(content=load_system_prompt()), HumanMessage(content=message)
    schema_cost = len(json.dumps(tool_schemas,ensure_ascii=False,separators=(',',':')).encode('utf-8')) if tool_schemas else 0
    fixed_cost = estimate_tokens([system,user]) + schema_cost
    require_budget(fixed_cost,settings.input_budget)
    kept, dropped = list(turns), 0
    while kept and fixed_cost + estimate_tokens([m for t in kept for m in t]) > settings.input_budget:
        dropped += len(kept.pop(0))
    messages = [system,*[m for t in kept for m in t],user]
    return PreparedChat(messages,estimate_tokens(messages)+schema_cost,dropped)


def prepare_tool_answer(prepared: PreparedChat, selection: AIMessage, result: ToolMessage | None, settings: Settings) -> list[BaseMessage]:
    suffix = [prepared.messages[-1]]
    if selection.tool_calls:
        if result is None:
            raise ServiceError('missing_tool_result',502,'工具结果未准备完成。')
        suffix.extend([selection,result])
    require_budget(estimate_tokens([prepared.messages[0],*suffix]),settings.input_budget)
    historical = prepared.messages[1:-1]
    while historical and estimate_tokens([prepared.messages[0],*historical,*suffix]) > settings.input_budget:
        next_human = next((i for i,m in enumerate(historical[1:],1) if isinstance(m,HumanMessage)),len(historical))
        historical = historical[next_human:]
    return [prepared.messages[0],*historical,*suffix]
