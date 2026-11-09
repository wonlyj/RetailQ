import pytest
from sqlalchemy import select,func
from retailq.config import Settings
from retailq.coordination import Coordinator
from retailq.errors import ServiceError
from retailq.models import Ticket,Conversation
from retailq.repositories import ChatRepository
from retailq.schemas import ChatRequest
from retailq.workflow.repository import WorkflowRepository
from retailq.workflow.actions import TicketActionService

pytestmark=[pytest.mark.mysql,pytest.mark.anyio]


async def test_bound_refund_requires_fixed_reason_and_one_ticket(test_database,tmp_path):
    from retailq.workflow.refunds import REFUND_REASONS
    settings=Settings(_env_file=None,mysql_port=3307,mysql_database='retailq_test',knowledge_data_dir=str(tmp_path/'knowledge'))
    repo=WorkflowRepository(test_database);turn=repo.start_turn(ChatRequest(user_id='alice',message='订单1001退款'))
    result=repo.finalize(turn,{'user_id':'alice','intent':'退款退货','route':'core','order_id':'1001',
        'answer':'依据规则核查本单。','status':'completed','actions':['ticket'],'gate':{'allowed':True},'evidence':{}})
    action=result.actions[0]
    assert action['subtype']=='refund' and action['order_id']=='1001' and tuple(action['reason_options'])==REFUND_REASONS
    service=TicketActionService(repo,ChatRepository(test_database),Coordinator.for_settings(settings,bind_knowledge=False),settings)
    with pytest.raises(ServiceError):await service.confirm(turn.conversation_id,'alice',action['action_id'],'绕过原因','退款')
    first=await service.confirm_refund(turn.conversation_id,'alice',action['action_id'],'不想要了','a'*32)
    second=await service.confirm_refund(turn.conversation_id,'alice',action['action_id'],'不想要了','a'*32)
    assert first.ticket['ticket_no']==second.ticket['ticket_no']
    with pytest.raises(ServiceError):await service.confirm_refund(turn.conversation_id,'alice',action['action_id'],'商品质量问题','a'*32)
    with test_database.sessions() as session:
        ticket=session.scalar(select(Ticket).where(Ticket.conversation_id==turn.conversation_id))
        assert ticket.ticket_type=='退款' and ticket.status=='待处理'
        assert '1001' in ticket.description and '不想要了' in ticket.description
        assert session.scalar(select(func.count()).select_from(Ticket).where(Ticket.conversation_id==turn.conversation_id))==1
        assert session.get(Conversation,turn.conversation_id).status=='进行中'
