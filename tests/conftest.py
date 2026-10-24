import pytest

from tests.support.database import test_database


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in (
        "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL", "CONTEXT_WINDOW_TOKENS",
        "MAX_OUTPUT_TOKENS", "TOKEN_SAFETY_MARGIN", "STRUCTURED_OUTPUT_METHOD",
        "MODEL_TIMEOUT_SECONDS",
        "MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD",
        "MYSQL_ROOT_PASSWORD", "TOOL_TIMEOUT_SECONDS", "TOOL_READ_RETRIES",
    ):
        monkeypatch.delenv(key, raising=False)
