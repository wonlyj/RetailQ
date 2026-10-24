from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from retailq.errors import ServiceError
from retailq.schemas import ChatRequest
from retailq.services.chat import PreparedTurn

router = APIRouter()


def get_chat_service(request: Request):
    if request.app.state.gateway is None:
        raise ServiceError('model_not_configured',503,'请先在本地 .env 中配置模型服务。')
    service = request.app.state.chat_service
    if service is None:
        raise ServiceError('database_not_configured',503,'请先配置数据库并启动 MySQL。')
    return service


async def get_prepared_turn(body: ChatRequest,request: Request):
    service = get_chat_service(request)
    turn = await service.prepare(body)
    try:
        yield turn
    finally:
        await service.release(turn)


@router.post('/api/chat/stream',response_class=EventSourceResponse)
async def chat_stream(request: Request,turn: Annotated[PreparedTurn,Depends(get_prepared_turn)]) -> AsyncIterator[ServerSentEvent]:
    service = get_chat_service(request)
    async with aclosing(service.stream(turn)) as stream:
        async for event in stream:
            yield ServerSentEvent(event=event.event,data=event.data)
