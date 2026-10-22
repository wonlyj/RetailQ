import json

import pytest
from fastapi.testclient import TestClient

from retailq.app import create_app
from retailq.config import Settings
from tests.support.servers import run_server
from tests.support.upstream import ControlledUpstream
from tests.test_gateway import settings_for


@pytest.mark.parametrize("expected", [
    {"order_id": "00123", "request_type": "exchange", "expected_solution": "换成42码"},
    {"order_id": None, "request_type": "unknown", "expected_solution": None},
    {"order_id": "C1", "request_type": "other", "expected_solution": "投诉"},
])
def test_extract_json_contract(expected):
    upstream = ControlledUpstream(text=json.dumps(expected))
    with run_server(upstream.app) as url, TestClient(create_app(settings_for(url))) as client:
        response = client.post("/api/after-sales/extract", json={"description": "售后描述"})
        assert response.status_code == 200
        assert response.json() == expected
        assert response.headers["content-type"] == "application/json"
        assert upstream.request_count == 1


def test_extract_validation_and_missing_config():
    upstream = ControlledUpstream()
    with run_server(upstream.app) as url, TestClient(create_app(settings_for(url))) as client:
        for payload in [{"description": " "}, {"description": "字" * 8001},
                        {"description": "退款", "other": "field"}, {"description": "字" * 8000}]:
            assert client.post("/api/after-sales/extract", json=payload).status_code == 422
        assert upstream.request_count == 0
    with TestClient(create_app(Settings(_env_file=None))) as client:
        assert client.post("/api/after-sales/extract", json={"description": "退款"}).status_code == 503


@pytest.mark.parametrize("kwargs,status,code", [
    ({"text": "not JSON"}, 502, "structured_output_invalid"),
    ({"status": 400}, 502, "upstream_error"),
    ({"status": 401}, 502, "upstream_auth_error"),
    ({"nonstream_delay": 0.5}, 504, "upstream_timeout"),
])
def test_extract_failure_status(kwargs, status, code):
    upstream = ControlledUpstream(**kwargs)
    with run_server(upstream.app) as url, TestClient(create_app(settings_for(url, model_timeout_seconds=0.2))) as client:
        response = client.post("/api/after-sales/extract", json={"description": "退款"})
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        assert "sensitive-upstream-secret" not in response.text
        assert upstream.request_count == 1
