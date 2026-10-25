import json

import httpx
import pytest
from sqlalchemy import update

from retailq.config import Settings
from retailq.errors import ServiceError
from retailq.knowledge.contracts import QAProposal
from retailq.models import Conversation


def snapshot():
    from retailq.knowledge.contracts import MiningMessage,ConversationSnapshot
    return ConversationSnapshot('source-1',1,2,(MiningMessage(1,'user','运费标准是什么？'),MiningMessage(2,'assistant','演示：普通订单满99元免运费。')))


def proposal(source='source-1',question='运费标准是什么？',answer='演示：普通订单满99元免运费。'):
    return QAProposal(source,question,answer,answer)


@pytest.mark.mysql
def test_only_closed_complete_snapshots_are_read(test_database):
    from retailq.repositories import ChatRepository
    from retailq.knowledge.repository import KnowledgeRepository
    r=ChatRepository(test_database)
    good=r.start_turn('closed',None,[],'运费标准是什么？')
    r.append_message(good,'closed','assistant','演示：满99元免运费。')
    incomplete=r.start_turn('incomplete',None,[],'没有回答')
    with test_database.sessions.begin() as session:
        session.execute(update(Conversation).where(Conversation.id.in_([good,incomplete])).values(status='已结束'))
    rows=KnowledgeRepository(test_database).closed_conversations()
    assert [row.conversation_id for row in rows]==[good]
    assert rows[0].source_ref==f'conversation:{good}:message:{rows[0].max_message_id}'


def test_batch_budget_keeps_whole_turns_and_source_ids():
    from retailq.knowledge.contracts import MiningMessage,ConversationSnapshot
    from retailq.knowledge.extraction import batch_conversations
    from retailq.knowledge.qa_gateway import extraction_cost
    messages=tuple(MiningMessage(i,'user' if i%2 else 'assistant',('问' if i%2 else '答')*30) for i in range(1,13))
    s=ConversationSnapshot('long-source',1,12,messages)
    budget=extraction_cost([ConversationSnapshot('long-source',1,12,messages[:2])])+40
    batches=batch_conversations([s],max_conversations=20,input_budget=budget)
    assert len(batches)>1
    assert [m.id for batch in batches for row in batch for m in row.messages]==list(range(1,13))
    assert all(extraction_cost(batch)<=budget for batch in batches)
    assert all(row.messages[0].role=='user' and row.messages[-1].role=='assistant' for batch in batches for row in batch)


@pytest.mark.mysql
@pytest.mark.parametrize('bad',[proposal(question='伪造问题'),proposal(answer='伪造答案。'),proposal(source='unknown')])
def test_question_and_evidence_must_belong_to_source(test_database,bad):
    from retailq.knowledge.repository import KnowledgeRepository
    from retailq.knowledge.extraction import extract_to_staging
    class Gateway:
        def extract(self,batch):return [bad]
    repository=KnowledgeRepository(test_database)
    with pytest.raises(ServiceError):extract_to_staging(repository,Gateway(),[snapshot()],'batch-1')
    assert repository.staging(['batch-1'])==[]


@pytest.mark.mysql
def test_repeated_batch_reuses_staging_rows(test_database):
    from retailq.knowledge.repository import KnowledgeRepository
    from retailq.knowledge.extraction import extract_to_staging
    class Gateway:
        def extract(self,batch):return [proposal()]
    r=KnowledgeRepository(test_database)
    first=extract_to_staging(r,Gateway(),[snapshot()],'batch-1')
    assert first==extract_to_staging(r,Gateway(),[snapshot()],'batch-1')
    assert len(r.staging(['batch-1']))==1


@pytest.mark.mysql
def test_extraction_does_not_hold_sql_connection_while_waiting(test_database):
    from retailq.knowledge.repository import KnowledgeRepository
    from retailq.knowledge.extraction import extract_to_staging
    class Gateway:
        def extract(self,batch):
            assert test_database.engine.pool.checkedout()==0
            return [proposal()]
    assert len(extract_to_staging(KnowledgeRepository(test_database),Gateway(),[snapshot()],'b'))==1


def gateway(content,reason='stop'):
    from retailq.knowledge.qa_gateway import OpenAIQAGateway
    def respond(request):
        body=json.loads(request.content)
        assert not body.get('tools')
        assert body['response_format']['type']=='json_object'
        return httpx.Response(200,json={'id':'test','object':'chat.completion','created':0,'model':'test-model','choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':reason}]})
    settings=Settings(_env_file=None,openai_base_url='http://qa.test/v1',openai_api_key='test',openai_model='test-model')
    return OpenAIQAGateway(settings,transport=httpx.MockTransport(respond))


@pytest.mark.parametrize('content,reason',[('{"items":[]','stop'),('{"items":[]}','length'),('{"items":[],"extra":1}','stop')])
def test_unknown_source_and_malformed_json_do_not_stage(content,reason):
    with gateway(content,reason) as model:
        with pytest.raises(ServiceError):model.extract([snapshot()])


def test_real_langchain_structured_extraction_protocol():
    content=json.dumps({'items':[{'source_ref':'source-1','question':'运费标准是什么？','answer':'演示：普通订单满99元免运费。','evidence':'演示：普通订单满99元免运费。'}]},ensure_ascii=False)
    with gateway(content) as model:
        assert model.extract([snapshot()])==[proposal()]
