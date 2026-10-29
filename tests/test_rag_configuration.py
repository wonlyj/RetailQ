from pathlib import Path

import pytest
from pydantic import ValidationError

from retailq.config import Settings


def test_milvus_uri_requires_network_endpoint():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, milvus_uri='knowledge.db')


def test_default_rag_configuration():
    settings = Settings(_env_file=None)
    assert settings.milvus_uri == 'http://127.0.0.1:19530'
    assert settings.knowledge_query_timeout_seconds == 30
    assert settings.rerank_model == 'BAAI/bge-reranker-v2-m3'


def test_rerank_key_reuse_requires_explicit_opt_in():
    settings = Settings(_env_file=None, embedding_api_key='embedding-secret')
    assert settings.effective_rerank_key == ''
    settings = Settings(_env_file=None, embedding_api_key='embedding-secret', rerank_reuse_embedding_key=True)
    assert settings.effective_rerank_key == 'embedding-secret'
    settings = Settings(_env_file=None, embedding_api_key='embedding-secret', rerank_api_key='rerank-secret', rerank_reuse_embedding_key=True)
    assert settings.effective_rerank_key == 'rerank-secret'


def test_minio_official_binary_is_pinned_in_both_compose_files():
    dockerfile = Path('docker/minio/Dockerfile').read_text(encoding='utf-8')
    assert '5708e53878085ab263c333b24fa7e59980425d64a9b085996b58799f9645e1db' in dockerfile
    for name in ('compose.yaml', 'compose.test.yaml'):
        compose = Path(name).read_text(encoding='utf-8')
        assert 'context: ./docker/minio' in compose
        assert 'minio/health/live' in compose


def test_milvus_test_scope_rejects_demo_and_non_test_collection():
    from tests.support.milvus import require_isolated_milvus

    require_isolated_milvus('http://127.0.0.1:19531', 'test_0123456789abcdef')
    with pytest.raises(ValueError):
        require_isolated_milvus('http://127.0.0.1:19530', 'test_0123456789abcdef')
    with pytest.raises(ValueError):
        require_isolated_milvus('http://127.0.0.1:19531', 'knowledge')


def test_standalone_fixture_uses_isolated_scope(standalone_store_settings):
    uri, collection = standalone_store_settings
    assert uri == 'http://127.0.0.1:19531'
    assert collection.startswith('test_')
