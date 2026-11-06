from dataclasses import asdict
from fastapi import APIRouter,Request
from retailq.errors import ServiceError
from retailq.tools.schemas import TicketInput,Identifier

router=APIRouter()


class ConfirmTicketRequest(TicketInput):
    user_id: Identifier


def _service(request):
    service=getattr(request.app.state,'ticket_actions',None)
    if service is None:raise ServiceError('workflow_not_configured',503,'工单操作尚未就绪。')
    return service


@router.post('/api/conversations/{conversation_id}/actions/{action_id}/ticket')
async def confirm_ticket(request:Request,conversation_id:int,action_id:str,body:ConfirmTicketRequest):
    return asdict(await _service(request).confirm(conversation_id,body.user_id,action_id,body.description,body.ticket_type))


@router.get('/api/conversations/{conversation_id}/actions/{action_id}/ticket')
async def ticket_status(request:Request,conversation_id:int,action_id:str,user_id:str):
    return asdict(await _service(request).get(conversation_id,user_id,action_id))
