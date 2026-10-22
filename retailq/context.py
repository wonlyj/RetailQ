from dataclasses import dataclass

from langchain_core.messages import BaseMessage, trim_messages

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
    return sum(len(message.content.encode("utf-8")) + 8 for message in messages)


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
