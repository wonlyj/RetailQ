import asyncio
from contextlib import aclosing

import pytest
from langchain_core.messages import HumanMessage

from retailq.config import Settings
from retailq.errors import ServiceError
from tests.support.servers import run_server
from tests.support.upstream import ControlledUpstream

pytestmark = pytest.mark.anyio


def settings_for(url, **kwargs):
    return Settings(_env_file=None, openai_base_url=url + "/v1", openai_api_key="fixture-secret",
                    openai_model="test-model", **kwargs)


async def test_chat_completions_contract():
    from retailq.gateway import OpenAIModelGateway
    upstream = ControlledUpstream()
    with run_server(upstream.app) as url:
        gateway = OpenAIModelGateway(settings_for(url))
        try:
            chunks = [chunk async for chunk in gateway.stream([HumanMessage(content="hi")])]
            assert "".join(chunk.text for chunk in chunks) == "您好，请问有什么购物问题？"
            assert chunks[-1].finish_reason == "stop"
            body, headers = upstream.requests[0]
            assert headers["authorization"] == "Bearer fixture-secret"
            assert body["model"] == "test-model" and body["stream"] is True
            assert body.get("max_completion_tokens", body.get("max_tokens")) == 1024
            assert "tools" not in body and "tool_choice" not in body
        finally:
            await gateway.aclose()


@pytest.mark.parametrize("method,expected_type", [("json_mode", "json_object"), ("json_schema", "json_schema")])
async def test_structured_method_contract(method, expected_type):
    from retailq.gateway import OpenAIModelGateway
    upstream = ControlledUpstream()
    with run_server(upstream.app) as url:
        gateway = OpenAIModelGateway(settings_for(url, structured_output_method=method))
        try:
            result = await gateway.extract([HumanMessage(content="return JSON")])
            assert result.model_dump() == {"order_id": "00123", "request_type": "exchange", "expected_solution": "换成42码"}
            body = upstream.requests[0][0]
            assert body["response_format"]["type"] == expected_type
            assert "tools" not in body and "tool_choice" not in body
        finally:
            await gateway.aclose()


@pytest.mark.parametrize("status,code", [(401, "upstream_auth_error"), (429, "upstream_rate_limited"),
                                       (400, "upstream_error"), (500, "upstream_error")])
async def test_gateway_failure_mapping(status, code):
    from retailq.gateway import OpenAIModelGateway
    upstream = ControlledUpstream(status=status)
    with run_server(upstream.app) as url:
        gateway = OpenAIModelGateway(settings_for(url))
        try:
            with pytest.raises(ServiceError) as exc:
                await gateway.extract([HumanMessage(content="JSON")])
            assert exc.value.code == code
            assert "secret" not in str(exc.value)
            assert upstream.request_count == 1
        finally:
            await gateway.aclose()


@pytest.mark.parametrize("text", ["not JSON", '{}',
    '{"order_id":null,"request_type":"fake","expected_solution":null}',
    '{"order_id":null,"request_type":"unknown","expected_solution":null,"extra":1}',
])
async def test_invalid_structure(text):
    from retailq.gateway import OpenAIModelGateway
    upstream = ControlledUpstream(text=text)
    with run_server(upstream.app) as url:
        gateway = OpenAIModelGateway(settings_for(url))
        try:
            with pytest.raises(ServiceError) as exc:
                await gateway.extract([HumanMessage(content="JSON")])
            assert exc.value.code == "structured_output_invalid"
            assert upstream.request_count == 1
        finally:
            await gateway.aclose()


@pytest.mark.parametrize("kwargs", [{"chunks": []}, {"tool": True}])
async def test_empty_or_tool_stream_rejected(kwargs):
    from retailq.gateway import OpenAIModelGateway
    upstream = ControlledUpstream(**kwargs)
    with run_server(upstream.app) as url:
        gateway = OpenAIModelGateway(settings_for(url))
        try:
            with pytest.raises(ServiceError) as exc:
                async for _ in gateway.stream([HumanMessage(content="hi")]):
                    pass
            assert exc.value.code == "unsupported_model_output"
        finally:
            await gateway.aclose()


async def test_timeout_after_partial_output_closes_upstream():
    from retailq.gateway import OpenAIModelGateway
    upstream = ControlledUpstream(gated=True)
    with run_server(upstream.app) as url:
        gateway = OpenAIModelGateway(settings_for(url, model_timeout_seconds=0.25))
        try:
            with pytest.raises(ServiceError) as exc:
                async for _ in gateway.stream([HumanMessage(content="hi")]):
                    pass
            assert exc.value.status_code == 504
            assert await asyncio.to_thread(upstream.closed.wait, 2)
        finally:
            upstream.release.set()
            await gateway.aclose()


async def test_consumer_closes_stream_without_finishing():
    from retailq.gateway import OpenAIModelGateway
    upstream = ControlledUpstream(gated=True)
    with run_server(upstream.app) as url:
        gateway = OpenAIModelGateway(settings_for(url))
        try:
            async with aclosing(gateway.stream([HumanMessage(content="hi")])) as stream:
                assert (await anext(stream)).text == "您好"
            assert await asyncio.to_thread(upstream.closed.wait, 2)
            assert not upstream.finished.is_set()
        finally:
            upstream.release.set()
            await gateway.aclose()


async def test_library_must_not_fallback_to_tool_calling():
    from retailq.gateway import OpenAIModelGateway
    settings = Settings(_env_file=None, openai_base_url="http://127.0.0.1:1/v1", openai_api_key="fixture",
                        openai_model="gpt-3.5-turbo", structured_output_method="json_schema")
    gateway = OpenAIModelGateway(settings)
    try:
        with pytest.raises(ServiceError) as exc:
            await gateway.extract([HumanMessage(content="JSON")])
        assert exc.value.code == "structured_mode_unsupported"
    finally:
        await gateway.aclose()
