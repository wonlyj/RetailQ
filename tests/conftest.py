import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in (
        "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL", "CONTEXT_WINDOW_TOKENS",
        "MAX_OUTPUT_TOKENS", "TOKEN_SAFETY_MARGIN", "STRUCTURED_OUTPUT_METHOD",
        "MODEL_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
