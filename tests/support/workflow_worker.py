"""Owned fault-test child. Only touches the existing isolated 3307 database."""
import argparse
import json
import os
from pathlib import Path
from fastapi.testclient import TestClient
from retailq.app import create_app
from tests.support.database import chat_settings
from tests.support.servers import run_server
from tests.support.workflow_upstream import WorkflowUpstream
from tests.test_chat_api import parse_sse


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--checkpoint',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--phase',choices=('after-tool','after-finalize','continue'),required=True)
    parser.add_argument('--cid',type=int)
    args=parser.parse_args()
    peer=WorkflowUpstream('one' if args.phase=='after-tool' else 'zero')
    peer.intent='退款退货' if args.phase=='after-finalize' else '订单'
    def save(data):Path(args.output).write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    with run_server(peer.app) as url:
        settings=chat_settings(url,workflow_checkpoint_path=args.checkpoint)
        assert (settings.mysql_port,settings.mysql_database)==(3307,'retailq_test')
        with TestClient(create_app(settings)) as client:
            service=client.app.state.chat_service
            if args.phase=='after-tool':
                original=service.workflow_repository.record_tool_result
                def crash(turn,call_id,payload):
                    original(turn,call_id,payload)
                    save({'cid':turn.conversation_id,'turn_id':turn.turn_id})
                    os._exit(0)
                service.workflow_repository.record_tool_result=crash
            elif args.phase=='after-finalize':
                from tests.test_rag_answer import retrieval
                service.runtime.chat_repository.rag_retrieve=lambda q:retrieval(0)
                class Policy:
                    def allows(self,result):return False
                service.runtime.policy=Policy()
                original=service.workflow_repository.finalize
                def crash(turn,state):
                    original(turn,state)
                    save({'cid':turn.conversation_id,'turn_id':turn.turn_id})
                    os._exit(0)
                service.workflow_repository.finalize=crash
            body={'user_id':'fault-test','message':'新的订单问题1001' if args.phase=='continue' else '订单1001的查询问题'}
            if args.cid:body['conversation_id']=args.cid
            events=parse_sse(client.post('/api/chat/stream',json=body).text)
            if args.phase!='continue':raise AssertionError('fault hook did not execute')
            decision=next(b for b in peer.requests if 'tools' in b)
            save({'done':events[-1][0]=='done','new_question_occurrences':sum(
                m.get('content')=='新的订单问题1001' for m in decision['messages'])})


if __name__=='__main__':main()
