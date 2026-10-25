import pytest
import retailq.knowledge.models  # Load all six mappings even in isolated tests.

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
        "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BATCH_SIZE",
        "EMBEDDING_TIMEOUT_SECONDS", "EMBEDDING_RETRIES", "KNOWLEDGE_ENABLED", "KNOWLEDGE_SCHEDULE_ENABLED",
        "KNOWLEDGE_DATA_DIR", "KNOWLEDGE_TOKENIZER_PATH", "KNOWLEDGE_TOP_K", "KNOWLEDGE_MIN_SCORE",
        "KNOWLEDGE_CHUNK_TARGET", "KNOWLEDGE_CHUNK_OVERLAP", "KNOWLEDGE_SCHEDULE_HOUR", "KNOWLEDGE_SCHEDULE_MINUTE",
        "KNOWLEDGE_TIMEZONE", "KNOWLEDGE_DEDUP_MIN_SCORE", "KNOWLEDGE_DEDUP_CANDIDATE_K",
    ):
        monkeypatch.delenv(key, raising=False)
