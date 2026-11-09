from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Annotated
from fastapi import APIRouter,Request,Depends
from fastapi.sse import EventSourceResponse,ServerSentEvent
from retailq.api.chat import get_chat_service
from retailq.workflow.pending import ResumeOrderRequest
from retailq.workflow.service import PreparedWorkflowTurn

router=APIRouter()


@router.get('/api/conversations/{conversation_id}/pending')
async def pending(request:Request,conversation_id:int,user_id:str):
    return {'pending':await get_chat_service(request).pending(conversation_id,user_id)}


@router.get('/api/conversations/{conversation_id}/turns/{turn_id}')
async def status(request:Request,conversation_id:int,turn_id:str,user_id:str):
    return await get_chat_service(request).turn_status(conversation_id,turn_id,user_id)


async def prepare_resume(request:Request,conversation_id:int,turn_id:str,body:ResumeOrderRequest):
    service=get_chat_service(request)
    turn=await service.prepare_resume(conversation_id,turn_id,body)
    try:yield turn
    finally:await service.release(turn)


@router.post('/api/conversations/{conversation_id}/turns/{turn_id}/resume',response_class=EventSourceResponse)
async def resume(request:Request,turn:Annotated[PreparedWorkflowTurn,Depends(prepare_resume)]) -> AsyncIterator[ServerSentEvent]:
    async with aclosing(get_chat_service(request).stream(turn)) as events:
        async for event in events:yield ServerSentEvent(event=event.event,data=event.data)
