from hashlib import sha256
import json

import pytest
from fastapi.testclient import TestClient

from retailq.app import create_app
from retailq.models import Conversation, Message
from tests.support.database import chat_settings


@pytest.mark.mysql
def test_sources_require_conversation_owner(test_database, tmp_path):
    content = '# 配送\n## 运费\n演示运费10元。\n'
    encoded = content.encode('utf-8')
    digest = sha256(encoded).hexdigest()
    key = 'shipping.md'
    archive = f'sources/{sha256(key.encode()).hexdigest()}/{digest}.md'
    path = tmp_path/'corpus'/archive
    path.parent.mkdir(parents=True)
    path.write_bytes(encoded)
    citation = {'number':1, 'chunk_id':9007199254740993, 'relevance_rank':1, 'category':'配送',
                'section_path':'配送 / 运费', 'questions':'运费多少', 'text':'演示运费10元。',
                'source_key':key, 'source_version':digest, 'archive':archive, 'line_start':2, 'line_end':3}
    with test_database.sessions.begin() as session:
        conversation = Conversation(user_id='Alice'); session.add(conversation); session.flush(); cid = conversation.id
        assistant = Message(conversation_id=cid, role='assistant', content='演示运费10元[1]。')
        session.add(assistant); session.flush()
        receipt = {'rag_evidence': {'bundle_hash':'a'*64, 'citations':[citation]},
                   'rag_finalization': {'assistant_message_id':assistant.id, 'low_confidence_question_id':None}}
        session.add(Message(conversation_id=cid, role='tool', tool_call_id='call1', content=json.dumps(receipt,ensure_ascii=False)))
        other = dict(citation, chunk_id=42, text='另一次审核答案', source_key='review', source_version='', archive='', line_start=None, line_end=None)
        second = Message(conversation_id=cid, role='assistant', content='另一次审核答案[1]。')
        session.add(second); session.flush()
        receipt2 = {'rag_evidence': {'bundle_hash':'b'*64, 'citations':[other]},
                    'rag_finalization': {'assistant_message_id':second.id, 'low_confidence_question_id':None}}
        session.add(Message(conversation_id=cid, role='tool', tool_call_id='call2', content=json.dumps(receipt2,ensure_ascii=False)))
    app = create_app(chat_settings('http://127.0.0.1:9', knowledge_data_dir=str(tmp_path)))
    with TestClient(app) as client:
        base = f'/api/conversations/{cid}/tool-calls/call1/sources'
        assert client.get(base, params={'user_id':'alice'}).status_code == 404
        response = client.get(base, params={'user_id':'Alice'})
        assert response.status_code == 200
        assert response.json()['citations'][0]['chunk_id'] == '9007199254740993'
        second_response = client.get(f'/api/conversations/{cid}/tool-calls/call2/sources', params={'user_id':'Alice'})
        assert second_response.json()['citations'][0]['text'] == '另一次审核答案'
        assert response.json()['citations'][0]['text'] == '演示运费10元。'
        document = client.get(base+'/1/document', params={'user_id':'Alice'})
        assert document.status_code == 200 and document.json()['text'] == content
        assert document.json()['anchor']['start'] == 2
        path.unlink()
        assert client.get(base+'/1/document', params={'user_id':'Alice'}).status_code == 404


def test_source_path_traversal_and_tampered_archive_are_rejected(tmp_path):
    from retailq.api.source_schemas import CitationRecord
    from retailq.api.sources import load_source_document
    from retailq.errors import ServiceError
    content = '# 内容\n<script>alert(1)</script>\n'
    digest = sha256(content.encode('utf-8')).hexdigest()
    key = 'safe.md'
    archive = f'sources/{sha256(key.encode()).hexdigest()}/{digest}.md'
    path = tmp_path/'corpus'/archive
    path.parent.mkdir(parents=True)
    path.write_text(content,encoding='utf-8',newline='')
    data = {'number':1,'chunk_id':1,'relevance_rank':1,'category':'分类','section_path':'内容',
            'questions':'问题','text':'<script>alert(1)</script>','source_key':key,'source_version':digest,
            'archive':archive,'line_start':1,'line_end':2}
    item = CitationRecord.model_validate(data)
    assert load_source_document(tmp_path, item).text == content
    with pytest.raises(ServiceError): load_source_document(tmp_path, item.model_copy(update={'source_key':'../secret'}))
    path.write_text('改动后内容', encoding='utf-8')
    with pytest.raises(ServiceError): load_source_document(tmp_path, item)
