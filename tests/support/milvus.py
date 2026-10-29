"""Guard all integration-test collection cleanup against the demo server."""
import re
import uuid
from urllib.parse import urlsplit

import pytest


def require_isolated_milvus(uri: str, collection: str) -> None:
    parsed = urlsplit(uri)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')
            or parsed.port != 19531 or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment
            or not re.fullmatch(r'test_[0-9a-f]{16,32}', collection)):
        raise ValueError('Milvus integration tests require isolated port 19531 and a test collection')


@pytest.fixture
def standalone_store_settings():
    uri = 'http://127.0.0.1:19531'
    collection = 'test_' + uuid.uuid4().hex
    require_isolated_milvus(uri, collection)
    return uri, collection
