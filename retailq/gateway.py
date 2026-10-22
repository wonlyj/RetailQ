import asyncio
import json
import warnings
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Protocol

import httpx
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI
from openai import APITimeoutError, AuthenticationError, RateLimitError
from pydantic import ValidationError

from retailq.config import Settings
from retailq.errors import ServiceError
from retailq.schemas import AfterSalesResult


@dataclass(frozen=True)
class ModelChunk:
    text: str = ""
    finish_reason: str | None = None


class ModelGateway(Protocol):
    def stream(self, messages: list[BaseMessage]) -> AsyncIterator[ModelChunk]: ...
    async def extract(self, messages: list[BaseMessage]) -> AfterSalesResult: ...
    async def aclose(self) -> None: ...


def public_error(exc: Exception) -> ServiceError:
    if isinstance(exc, ServiceError):
        return exc
    if isinstance(exc, (TimeoutError, APITimeoutError)):
        return ServiceError("upstream_timeout", 504, "模型响应超时，请稍后重试。")
    if isinstance(exc, AuthenticationError):
        return ServiceError("upstream_auth_error", 502, "模型服务鉴权失败，请检查本地配置。")
    if isinstance(exc, RateLimitError):
        return ServiceError("upstream_rate_limited", 502, "模型服务暂时限流，请稍后重试。")
    if isinstance(exc, (OutputParserException, ValidationError)):
        return ServiceError("structured_output_invalid", 502, "模型未返回有效的售后结构，请重试或核查模型能力。")
    return ServiceError("upstream_error", 502, "模型服务调用失败，请核查服务状态与协议支持。")


def unsupported() -> ServiceError:
    return ServiceError("unsupported_model_output", 502, "模型未返回可用的纯文本回复。")


class OpenAIModelGateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        # These clients belong to this gateway. Explicit ownership avoids touching
        # private SDK attributes and closes both sync and async connection pools.
        self.http = httpx.Client(trust_env=False)
        self.http_async = httpx.AsyncClient(trust_env=False)
        self.model = ChatOpenAI(
            model=settings.openai_model, base_url=settings.openai_base_url,
            api_key=settings.openai_api_key, max_tokens=settings.max_output_tokens,
            timeout=settings.model_timeout_seconds, max_retries=0,
            use_responses_api=False, stream_usage=False,
            http_client=self.http, http_async_client=self.http_async,
        )

    async def aclose(self) -> None:
        await self.http_async.aclose()
        self.http.close()

    async def stream(self, messages: list[BaseMessage]) -> AsyncIterator[ModelChunk]:
        seen_text = False
        seen_finish = False
        try:
            async with asyncio.timeout(self.settings.model_timeout_seconds):
                async with aclosing(self.model.astream(messages)) as stream:
                    async for chunk in stream:
                        if chunk.tool_calls or chunk.tool_call_chunks or chunk.additional_kwargs.get("tool_calls"):
                            raise unsupported()
                        if not isinstance(chunk.content, str):
                            raise unsupported()
                        reason = chunk.response_metadata.get("finish_reason")
                        if reason in ("tool_calls", "function_call"):
                            raise unsupported()
                        if chunk.content:
                            seen_text = True
                        if isinstance(reason, str) and reason:
                            seen_finish = True
                        if chunk.content or reason:
                            yield ModelChunk(chunk.content, reason)
                if not seen_text:
                    raise unsupported()
                if not seen_finish:
                    raise ServiceError("incomplete_upstream_stream", 502, "模型回复未完整结束，请重试。")
        except Exception as exc:
            raise public_error(exc) from None

    async def extract(self, messages: list[BaseMessage]) -> AfterSalesResult:
        try:
            # Some LangChain versions warn and silently substitute tool calling.
            # Treat that specific fallback as unsupported before any request.
            with warnings.catch_warnings():
                warnings.filterwarnings("error", message="(?s).*Overriding to method=", category=UserWarning)
                try:
                    structured = self.model.with_structured_output(
                        AfterSalesResult, method=self.settings.structured_output_method, include_raw=True,
                    )
                except UserWarning:
                    raise ServiceError("structured_mode_unsupported", 502, "当前模型不支持指定的无工具结构化模式，请核查配置。") from None
            async with asyncio.timeout(self.settings.model_timeout_seconds):
                output = await structured.ainvoke(messages)
            if output.get("parsing_error") or output.get("parsed") is None:
                raise ServiceError("structured_output_invalid", 502, "模型未返回有效的售后结构，请核查模型能力。")
            raw = output["raw"]
            if raw.tool_calls or raw.additional_kwargs.get("tool_calls"):
                raise unsupported()
            # LangChain's JSON parser can repair partial JSON. Validate the raw
            # syntax as well, while retaining with_structured_output as parser.
            try:
                strict_result = AfterSalesResult.model_validate(json.loads(raw.content))
                parsed = AfterSalesResult.model_validate(output["parsed"])
                if strict_result != parsed:
                    raise ValueError("raw and parsed results differ")
            except (ValueError, TypeError):
                raise ServiceError("structured_output_invalid", 502, "模型未返回完整、单一且有效的售后 JSON。") from None
            return parsed
        except Exception as exc:
            raise public_error(exc) from None
