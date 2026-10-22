import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from retailq.app import create_app
from retailq.config import Settings
from tests.support.servers import run_server
from tests.support.upstream import ControlledUpstream
from tests.test_gateway import settings_for


def parse_sse(text):
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        event = None
        data = []
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        if data:
            events.append((event, json.loads("\n".join(data))))
    return events


def test_chat_sse_contract_and_client_history():
    upstream = ControlledUpstream(chunks=["您", "好\n"])
    with run_server(upstream.app) as url, TestClient(create_app(settings_for(url))) as client:
        response = client.post("/api/chat/stream", json={"message": "订单00123"})
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(response.text)
        assert [e[0] for e in events] == ["meta", "token", "token", "done"]
        assert events[1:3] == [("token", {"text": "您"}), ("token", {"text": "好\n"})]
        assert events[0][1]["dropped_history_messages"] == 0
        assert events[-1][1] == {"finish_reason": "stop"}
        history = [{"role": "user", "content": "订单00123"}, {"role": "assistant", "content": "您好"}]
        client.post("/api/chat/stream", json={"message": "刚才订单号？", "history": history})
        assert [m["content"] for m in upstream.requests[1][0]["messages"]][1:] == ["订单00123", "您好", "刚才订单号？"]
        client.post("/api/chat/stream", json={"message": "新会话"})
        assert len(upstream.requests[2][0]["messages"]) == 2


def test_pre_stream_errors_do_not_call_model():
    upstream = ControlledUpstream()
    with run_server(upstream.app) as url, TestClient(create_app(settings_for(url))) as client:
        assert client.post("/api/chat/stream", json={"message": ""}).status_code == 422
        assert client.post("/api/chat/stream", json={"message": "字" * 8000}).status_code == 422
        assert upstream.request_count == 0
    with TestClient(create_app(Settings(_env_file=None))) as client:
        response = client.post("/api/chat/stream", json={"message": "你好"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "model_not_configured"


@pytest.mark.parametrize("kwargs,last_event", [
    ({"status": 500}, "error"), ({"chunks": []}, "error"),
    ({"tool": True}, "error"), ({"finish_reason": "length"}, "done"),
    ({"gated": True}, "error"),
])
def test_stream_error_and_finish_reason(kwargs, last_event):
    upstream = ControlledUpstream(**kwargs)
    with run_server(upstream.app) as url, TestClient(create_app(settings_for(url, model_timeout_seconds=0.5))) as client:
        events = parse_sse(client.post("/api/chat/stream", json={"message": "你好"}).text)
        assert events[-1][0] == last_event
        if last_event == "error":
            assert not any(event == "done" for event, _ in events)
            assert "sensitive-upstream-secret" not in json.dumps(events)
        else:
            assert events[-1][1]["finish_reason"] == "length"
        assert upstream.request_count == 1


@pytest.mark.anyio
async def test_first_token_arrives_before_completion_and_disconnect():
    upstream = ControlledUpstream(gated=True)
    with run_server(upstream.app) as upstream_url, run_server(create_app(settings_for(upstream_url))) as api_url:
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                async with client.stream("POST", api_url + "/api/chat/stream", json={"message": "你好"}) as response:
                    assert response.status_code == 200
                    lines = response.aiter_lines()
                    block = []
                    async with asyncio.timeout(3):
                        async for line in lines:
                            if line:
                                block.append(line)
                            else:
                                events = parse_sse("\n".join(block) + "\n\n")
                                block = []
                                if events and events[0][0] == "token":
                                    assert events[0][1] == {"text": "您好"}
                                    break
                    assert not upstream.finished.is_set()
                assert await asyncio.to_thread(upstream.closed.wait, 2)
        finally:
            upstream.release.set()
