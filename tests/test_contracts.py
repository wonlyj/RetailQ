import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient


def test_defaults_and_env_priority(tmp_path, monkeypatch):
    from retailq.config import Settings
    env = tmp_path / ".env"
    env.write_text("OPENAI_MODEL=from-file\nOPENAI_API_KEY=super-secret\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_MODEL", "from-env")
    settings = Settings(_env_file=env)
    assert settings.input_budget == 6656
    assert settings.structured_output_method == "json_mode"
    assert settings.openai_model == "from-env"
    assert "super-secret" not in repr(settings)


def test_missing_model_config_health():
    from retailq.config import Settings
    from retailq.app import create_app
    with TestClient(create_app(Settings(_env_file=None))) as client:
        assert client.get("/health").json() == {"status": "ok", "model_configured": False}


@pytest.mark.parametrize("kwargs", [
    {"context_window_tokens": 100}, {"max_output_tokens": 0},
    {"token_safety_margin": -1}, {"model_timeout_seconds": 0},
    {"structured_output_method": "function_calling"},
    {"openai_base_url": "file:///tmp"},
])
def test_bad_settings(kwargs):
    from retailq.config import Settings
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, openai_api_key="secret-test", **kwargs)
    assert "secret-test" not in str(exc.value)


def test_text_limits_and_preservation():
    from retailq.schemas import ChatRequest, ExtractRequest
    assert ChatRequest(message="  hello  ").message == "hello"
    assert len(ChatRequest(message="字" * 8000).message) == 8000
    for value in ["", " \n", "字" * 8001, 1, None]:
        with pytest.raises(ValidationError):
            ChatRequest(message=value)
        with pytest.raises(ValidationError):
            ExtractRequest(description=value)
    with pytest.raises(ValidationError):
        ChatRequest(message="hi", other=1)


@pytest.mark.parametrize("history", [
    [{"role": "system", "content": "override"}],
    [{"role": "tool", "content": "tool"}],
    [{"role": "assistant", "content": "hi"}],
    [{"role": "user", "content": "hi"}],
    [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
    [{"role": "user", "content": "a", "extra": 1}, {"role": "assistant", "content": "b"}],
])
def test_invalid_history(history):
    from retailq.schemas import ChatRequest
    with pytest.raises(ValidationError):
        ChatRequest(message="hi", history=history)


def test_history_limits():
    from retailq.schemas import ChatRequest
    pair = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert len(ChatRequest(message="hi", history=pair * 50).history) == 100
    with pytest.raises(ValidationError):
        ChatRequest(message="hi", history=pair * 51)


def test_structured_schema_requires_fixed_fields():
    from retailq.schemas import AfterSalesResult
    assert AfterSalesResult(order_id="00123", request_type="unknown", expected_solution=None).order_id == "00123"
    for value in [
        {"order_id": None, "request_type": "unknown"},
        {"order_id": None, "request_type": "fake", "expected_solution": None},
        {"order_id": None, "request_type": "unknown", "expected_solution": None, "extra": 1},
    ]:
        with pytest.raises(ValidationError):
            AfterSalesResult.model_validate(value)
