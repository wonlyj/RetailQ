import json
import pytest
from sqlalchemy import select

from retailq.errors import ServiceError
from retailq.knowledge.rag_models import LowConfidenceQuestion
from retailq.knowledge.rag_repository import RAGRepository
from retailq.models import Conversation, Message


@pytest.mark.mysql
def test_refusal_finalization_is_atomic_and_idempotent(test_database):
    with test_database.sessions.begin() as session:
        conversation = Conversation(user_id='Alice')
        session.add(conversation); session.flush(); cid = conversation.id
        session.add(Message(conversation_id=cid, role='user', content='型号 XYZ 能飞吗？'))
        session.add(Message(conversation_id=cid, role='assistant', content=None,
                            tool_calls=[{'id':'Call1', 'function':{'name':'query_faq'}}]))
        session.add(Message(conversation_id=cid, role='tool', tool_call_id='Call1', content=json.dumps({'found':False,'items':[]})))
    repository = RAGRepository(test_database)
    first = repository.finalize(cid, 'Alice', 'Call1', answer='知识库没有相关信息，无法回答。', source='retrieval_low_conf', reason='无证据')
    second = repository.finalize(cid, 'Alice', 'Call1', answer='知识库没有相关信息，无法回答。', source='retrieval_low_conf', reason='无证据')
    assert first == second
    with test_database.sessions() as session:
        questions = session.scalars(select(LowConfidenceQuestion)).all()
        assert len(questions) == 1 and questions[0].raw_question == '型号 XYZ 能飞吗？'
    with pytest.raises(ServiceError):
        repository.finalize(cid, 'alice', 'Call1', answer='知识库没有相关信息，无法回答。', source='retrieval_low_conf', reason='无证据')
    with pytest.raises(ServiceError):
        repository.finalize(cid, 'Alice', 'call1', answer='知识库没有相关信息，无法回答。', source='retrieval_low_conf', reason='无证据')
    with test_database.sessions.begin() as session:
        session.add(Message(conversation_id=cid, role='user', content='再问一次'))
        session.add(Message(conversation_id=cid, role='assistant', content=None,
                            tool_calls=[{'id':'Call2', 'function':{'name':'query_faq'}}]))
        session.add(Message(conversation_id=cid, role='tool', tool_call_id='Call2', content='{"found":false,"items":[]}'))
    third = repository.finalize(cid, 'Alice', 'Call2', answer='无法回答。', source='retrieval_low_conf', reason='无证据')
    assert third != first
    with test_database.sessions() as session:
        questions = session.scalars(select(LowConfidenceQuestion).order_by(LowConfidenceQuestion.id)).all()
        assert [row.raw_question for row in questions] == ['型号 XYZ 能飞吗？', '再问一次']


@pytest.mark.mysql
def test_refusal_rolls_back_if_pool_write_fails(test_database, monkeypatch):
    with test_database.sessions.begin() as session:
        conversation = Conversation(user_id='user')
        session.add(conversation); session.flush(); cid = conversation.id
        session.add(Message(conversation_id=cid, role='user', content='不存在的问题'))
        session.add(Message(conversation_id=cid, role='assistant', content=None,
                            tool_calls=[{'id':'T1', 'function':{'name':'query_faq'}}]))
        session.add(Message(conversation_id=cid, role='tool', tool_call_id='T1', content='{"found":false,"items":[]}'))
    repository = RAGRepository(test_database)
    def fail(*args): raise RuntimeError('simulated failed pool write')
    monkeypatch.setattr(repository, '_add_pool', fail)
    with pytest.raises(RuntimeError):
        repository.finalize(cid, 'user', 'T1', answer='无法回答', source='self_check', reason='证据不足')
    with test_database.sessions() as session:
        messages = session.scalars(select(Message).where(Message.conversation_id == cid, Message.role == 'assistant')).all()
        assert len(messages) == 1
        assert not session.scalars(select(LowConfidenceQuestion)).all()


@pytest.mark.mysql
def test_tampered_pool_receipt_is_not_accepted(test_database):
    with test_database.sessions.begin() as session:
        conversation = Conversation(user_id='u')
        session.add(conversation); session.flush(); cid = conversation.id
        session.add(Message(conversation_id=cid, role='user', content='原始问题'))
        session.add(Message(conversation_id=cid, role='assistant', content=None,
                            tool_calls=[{'id':'T1', 'function':{'name':'query_faq'}}]))
        session.add(Message(conversation_id=cid, role='tool', tool_call_id='T1', content='{"found":false,"items":[]}'))
    repository = RAGRepository(test_database)
    repository.finalize(cid, 'u', 'T1', answer='无法回答', source='retrieval_low_conf', reason='无证据')
    with test_database.sessions.begin() as session:
        pool = session.scalar(select(LowConfidenceQuestion))
        pool.raw_question = '被篡改的问题'
    with pytest.raises(ServiceError):
        repository.finalize(cid, 'u', 'T1', answer='无法回答', source='retrieval_low_conf', reason='无证据')
