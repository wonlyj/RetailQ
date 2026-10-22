from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import anyio
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import ValidationError

from retailq.config import Settings
from retailq.context import PreparedChat, prepare_chat, prepare_extraction
from retailq.errors import ServiceError
from retailq.gateway import ModelGateway, OpenAIModelGateway, public_error, unsupported
from retailq.schemas import AfterSalesResult, ChatRequest, ExtractRequest


def get_gateway(request: Request) -> ModelGateway:
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is None:
        raise ServiceError("model_not_configured", 503, "请先在本地 .env 中配置模型服务。")
    return gateway


def get_prepared_chat(body: ChatRequest, request: Request) -> PreparedChat:
    # A dependency runs before SSE starts, so input/budget errors retain HTTP 422.
    return prepare_chat(body, request.app.state.settings)


def create_app(settings: Settings | None = None) -> FastAPI:
    try:
        settings = settings if settings is not None else Settings()
    except ValidationError:
        raise RuntimeError("应用配置无效，请检查 .env 配置。") from None
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        gateway = OpenAIModelGateway(settings) if settings.is_ready else None
        application.state.gateway = gateway
        try:
            yield
        finally:
            if gateway is not None:
                with anyio.move_on_after(5, shield=True):
                    await gateway.aclose()

    application = FastAPI(title="RetailQ", version="0.1.0", lifespan=lifespan)
    application.state.settings = settings

    @application.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.public_message}}, status_code=exc.status_code)

    @application.get("/health")
    async def health():
        return {"status": "ok", "model_configured": settings.is_ready}

    @application.post("/api/chat/stream", response_class=EventSourceResponse)
    async def chat_stream(
        prepared: Annotated[PreparedChat, Depends(get_prepared_chat)],
        gateway: Annotated[ModelGateway, Depends(get_gateway)],
    ) -> AsyncIterator[ServerSentEvent]:
        yield ServerSentEvent(event="meta", data={
            "input_tokens_estimate": prepared.input_tokens_estimate,
            "dropped_history_messages": prepared.dropped_history_messages,
        })
        stream = gateway.stream(prepared.messages)
        seen_text = False
        finish_reason = "stop"
        try:
            async for chunk in stream:
                if chunk.text:
                    seen_text = True
                    yield ServerSentEvent(event="token", data={"text": chunk.text})
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason
            if not seen_text:
                raise unsupported()
            yield ServerSentEvent(event="done", data={"finish_reason": finish_reason})
        except Exception as exc:
            error = public_error(exc)
            yield ServerSentEvent(event="error", data={"code": error.code, "message": error.public_message})
        finally:
            with anyio.move_on_after(5, shield=True):
                await stream.aclose()

    @application.post("/api/after-sales/extract", response_model=AfterSalesResult)
    async def extract_after_sales(
        body: ExtractRequest, request: Request,
        gateway: Annotated[ModelGateway, Depends(get_gateway)],
    ) -> AfterSalesResult:
        messages = prepare_extraction(body.description, request.app.state.settings)
        return await gateway.extract(messages)

    return application


app = create_app()
