import json

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from retailq.errors import ServiceError
from retailq.models import Conversation, Message, Ticket
from retailq.schemas import HistoryMessage

pytestmark = pytest.mark.mysql


@pytest.fixture
def repository(test_database):
    from retailq.repositories import ChatRepository
    return ChatRepository(test_database)


def test_start_and_reload_turn(repository, test_database):
    cid = repository.start_turn('alice', None, [HistoryMessage(role='user', content='旧问题'),
        HistoryMessage(role='assistant', content='旧回答')], '新问题')
    records = repository.load_messages(cid, 'alice')
    assert [r.content for r in records] == ['旧问题', '旧回答', '新问题']
    assert [r.role for r in records] == ['user', 'assistant', 'user']
    assert test_database.engine.pool.checkedout() == 0
    with test_database.sessions() as session:
        assert session.get(Conversation, cid).status == '进行中'


def test_wrong_owner_and_missing_conversation(repository):
    cid = repository.start_turn('alice', None, [], '你好')
    for conv, user in [(cid, 'bob'), (999999, 'alice')]:
        with pytest.raises(ServiceError) as error:
            repository.start_turn(user, conv, [], '偷看')
        assert error.value.status_code == 404
        with pytest.raises(ServiceError):
            repository.load_messages(conv, user)
    assert len(repository.load_messages(cid, 'alice')) == 1
    with pytest.raises(ServiceError):
        repository.start_turn('alice', cid, [HistoryMessage(role='user', content='重复')], '新')


@pytest.mark.parametrize('impostor',['ALICE','álice'])
def test_user_ownership_uses_exact_identifiers(repository,impostor):
    cid = repository.start_turn('alice',None,[],'仅alice可见')
    for operation in [lambda:repository.load_messages(cid,impostor),
                      lambda:repository.start_turn(impostor,cid,[],'错误身份'),
                      lambda:repository.append_message(cid,impostor,'assistant','错误身份')]:
        with pytest.raises(ServiceError) as error:
            operation()
        assert error.value.status_code == 404
    assert len(repository.load_messages(cid,'alice')) == 1


def test_faq_literal_like_and_gap(repository):
    assert repository.search_faq('退货政策')
    assert repository.search_faq('运费')
    for keyword in ['邮费', '%', '_', '\\', "' OR 1=1 --"]:
        assert repository.search_faq(keyword) == []


def pending_ticket(repository):
    cid = repository.start_turn('alice', None, [], '订单1001延迟，请投诉')
    call = {'id': 'ticket_call_1', 'type': 'function', 'function': {
        'name': 'create_ticket', 'arguments': json.dumps({'description': '订单1001延迟', 'ticket_type': '投诉'}, ensure_ascii=False)}}
    repository.append_message(cid, 'alice', 'assistant', None, [call])
    return cid


def test_ticket_commit_receipt_atomic_and_repeat_safe(repository, test_database):
    cid = pending_ticket(repository)
    result = repository.create_ticket(cid, 'alice', '订单1001延迟', '投诉', 'ticket_call_1')
    assert len(result['ticket_no']) <= 32
    assert result['status'] == '待处理'
    again = repository.create_ticket(cid, 'alice', '订单1001延迟', '投诉', 'ticket_call_1')
    assert again == result
    with test_database.sessions() as session:
        assert session.get(Conversation, cid).status == '已转人工'
        assert session.scalar(select(func.count()).select_from(Ticket).where(Ticket.conversation_id == cid)) == 1
    from retailq.repositories import ChatRepository
    records = ChatRepository(test_database).load_messages(cid, 'alice')
    receipts = [r for r in records if r.role == 'tool']
    assert len(receipts) == 1
    assert json.loads(receipts[0].content)['data']['ticket_no'] == result['ticket_no']


def test_ticket_failure_rolls_back_both_status_and_receipt(repository, test_database):
    cid = pending_ticket(repository)
    def fail(session):
        raise OperationalError('forced', None, Exception('password-secret'))
    event.listen(Session, 'before_commit', fail)
    try:
        with pytest.raises(ServiceError) as error:
            repository.create_ticket(cid, 'alice', '订单1001延迟', '投诉', 'ticket_call_1')
        assert 'password-secret' not in error.value.public_message
    finally:
        event.remove(Session, 'before_commit', fail)
    with test_database.sessions() as session:
        assert session.get(Conversation, cid).status == '进行中'
        assert session.scalar(select(func.count()).select_from(Ticket).where(Ticket.conversation_id == cid)) == 0
        assert session.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == cid, Message.role == 'tool')) == 0
