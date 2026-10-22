import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from retailq.config import Settings
from retailq.schemas import ChatRequest
from retailq.errors import ServiceError


def test_literals_and_roles_preserved():
    from retailq.prompts import build_chat_messages
    text = "{price} {{x}} 中文😀"
    messages = build_chat_messages(ChatRequest(message=text), "客服{literal}")
    assert [m.type for m in messages] == ["system", "human"]
    assert messages[0].content == "客服{literal}"
    assert messages[1].content == text


def test_utf8_estimate():
    from retailq.context import estimate_tokens
    assert estimate_tokens([HumanMessage(content="中😀")]) == 15
    assert estimate_tokens([]) == 0


def test_budget_preserves_recent_pairs(monkeypatch):
    from retailq.context import prepare_chat
    monkeypatch.setattr("retailq.context.load_system_prompt", lambda: "客服")
    history = [{"role": role, "content": text} for role, text in
               [("user", "a"), ("assistant", "b"), ("user", "c"), ("assistant", "d")]]
    result = prepare_chat(ChatRequest(message="问", history=history), Settings(
        _env_file=None, context_window_tokens=1043, max_output_tokens=900, token_safety_margin=100,
    ))
    assert [m.content for m in result.messages] == ["客服", "c", "d", "问"]
    assert result.input_tokens_estimate == 43
    assert result.dropped_history_messages == 2


def test_one_token_less_drops_whole_pair(monkeypatch):
    from retailq.context import prepare_chat
    monkeypatch.setattr("retailq.context.load_system_prompt", lambda: "客服")
    request = ChatRequest(message="问", history=[
        {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"},
    ])
    result = prepare_chat(request, Settings(_env_file=None, context_window_tokens=1042,
                                           max_output_tokens=900, token_safety_margin=100))
    assert [m.content for m in result.messages] == ["客服", "问"]
    assert result.dropped_history_messages == 2


def test_fixed_input_over_budget(monkeypatch):
    from retailq.context import prepare_chat
    monkeypatch.setattr("retailq.context.load_system_prompt", lambda: "客服")
    settings = Settings(_env_file=None, context_window_tokens=1024,
                        max_output_tokens=900, token_safety_margin=100)
    with pytest.raises(ServiceError) as exc:
        prepare_chat(ChatRequest(message="问"), settings)
    assert exc.value.code == "input_budget_exceeded"
    assert exc.value.status_code == 422


def test_hundred_history_messages(monkeypatch):
    from retailq.context import prepare_chat
    monkeypatch.setattr("retailq.context.load_system_prompt", lambda: "客服")
    pairs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}] * 50
    result = prepare_chat(ChatRequest(message="问", history=pairs), Settings(
        _env_file=None, context_window_tokens=1043, max_output_tokens=900, token_safety_margin=100))
    assert result.dropped_history_messages == 98
    assert [m.type for m in result.messages] == ["system", "human", "ai", "human"]


@pytest.mark.parametrize("method", ["json_mode", "json_schema"])
def test_extract_schema_budget(method):
    from retailq.context import prepare_extraction
    settings = Settings(_env_file=None, context_window_tokens=1200, max_output_tokens=1024,
                        token_safety_margin=0, structured_output_method=method)
    with pytest.raises(ServiceError):
        prepare_extraction("我要换货", settings)
    messages = prepare_extraction("{description}订单00123", Settings(
        _env_file=None, context_window_tokens=32000, structured_output_method=method))
    assert messages[-1].content == "{description}订单00123"
    assert "order_id" in messages[0].content
