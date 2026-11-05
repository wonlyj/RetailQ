import pytest
import sys
import retailq.knowledge.models  # Load all six mappings even in isolated tests.
import retailq.knowledge.rag_models  # Register chapter-four mappings for schema checks.
import retailq.workflow.models

from tests.support.database import test_database
from tests.support.milvus import standalone_store_settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_owned_standalone_collections():
    module = sys.modules.get('tests.support.standalone_legacy')
    before = set(module.CREATED) if module else set()
    yield
    module = sys.modules.get('tests.support.standalone_legacy')
    if module is None: return
    created = module.CREATED - before
    if not created: return
    from pymilvus import MilvusClient
    from tests.support.milvus import require_isolated_milvus
    client = MilvusClient(module.URI)
    try:
        for name in created:
            require_isolated_milvus(module.URI, name)
            if client.has_collection(name): client.drop_collection(name)
    finally:
        client.close()
        module.CREATED.difference_update(created)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in (
        "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL", "CONTEXT_WINDOW_TOKENS",
        "MAX_OUTPUT_TOKENS", "TOKEN_SAFETY_MARGIN", "STRUCTURED_OUTPUT_METHOD",
        "MODEL_TIMEOUT_SECONDS",
        "MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD",
        "MYSQL_ROOT_PASSWORD", "TOOL_TIMEOUT_SECONDS", "TOOL_READ_RETRIES",
        "WORKFLOW_MAX_TOOL_STEPS", "WORKFLOW_MAX_MODEL_CALLS", "WORKFLOW_TURN_TIMEOUT_SECONDS",
        "WORKFLOW_MAX_TOTAL_TOKENS", "WORKFLOW_CHECKPOINT_PATH",
        "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_PROXY", "EMBEDDING_MODEL", "EMBEDDING_BATCH_SIZE",
        "EMBEDDING_TIMEOUT_SECONDS", "EMBEDDING_RETRIES", "KNOWLEDGE_ENABLED", "KNOWLEDGE_SCHEDULE_ENABLED",
        "KNOWLEDGE_DATA_DIR", "KNOWLEDGE_TOKENIZER_PATH", "KNOWLEDGE_TOP_K", "KNOWLEDGE_MIN_SCORE",
        "KNOWLEDGE_CHUNK_TARGET", "KNOWLEDGE_CHUNK_OVERLAP", "KNOWLEDGE_SCHEDULE_HOUR", "KNOWLEDGE_SCHEDULE_MINUTE",
        "KNOWLEDGE_TIMEZONE", "KNOWLEDGE_DEDUP_MIN_SCORE", "KNOWLEDGE_DEDUP_CANDIDATE_K",
        "MILVUS_URI", "MILVUS_COLLECTION_PREFIX", "KNOWLEDGE_QUERY_TIMEOUT_SECONDS",
        "RERANK_BASE_URL", "RERANK_API_KEY", "RERANK_PROXY", "RERANK_MODEL",
        "RERANK_TIMEOUT_SECONDS", "RAG_CONFIDENCE_PATH",
        "RERANK_REUSE_EMBEDDING_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
