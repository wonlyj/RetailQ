from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from retailq.app import create_app
from retailq.schemas import ChatRequest
from retailq.knowledge.answer import prepare_bundle
from retailq.workflow.repository import WorkflowRepository
from tests.support.database import chat_settings
from tests.test_rag_answer import retrieval

pytestmark=pytest.mark.mysql


def test_workflow_sources_are_owned_and_distinct_from_model_tool_calls(test_database,tmp_path):
    repo=WorkflowRepository(test_database)
    turn=repo.start_turn(ChatRequest(user_id='alice',message='运费'))
    bundle=prepare_bundle(retrieval(1),system_prompt='政策',input_budget=6000)
    final=repo.finalize(turn,{'user_id':'alice','answer':'10元[1]','status':'completed','actions':[],
        'evidence':{'citations':[asdict(c) for c in bundle.citations]}})
    app=create_app(chat_settings('http://127.0.0.1:9',knowledge_data_dir=str(tmp_path)))
    with TestClient(app) as client:
        app.state.workflow_repository=repo
        base=f'/api/conversations/{turn.conversation_id}/workflow-turns/{turn.turn_id}/sources'
        assert client.get(base,params={'user_id':'bob'}).status_code==404
        response=client.get(base,params={'user_id':'alice'})
        assert response.status_code==200
        assert response.json()['citations'][0]['number']==1
        assert client.get(base+'/1/document',params={'user_id':'alice'}).json()['source_key']=='review'
        assert client.get(base+'/2/document',params={'user_id':'alice'}).status_code==404
