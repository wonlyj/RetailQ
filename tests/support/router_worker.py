"""Owned cross-process interrupt fault test against 3307 only."""
import argparse
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from fastapi.testclient import TestClient
from retailq.app import create_app
from tests.support.database import chat_settings
from tests.support.servers import run_server
from tests.support.workflow_upstream import WorkflowUpstream
from tests.test_chat_api import parse_sse


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--phase',choices=('pause-crash','resume'),required=True)
    parser.add_argument('--checkpoint',required=True);parser.add_argument('--marker',required=True)
    args=parser.parse_args();path=Path(args.marker)
    peer=WorkflowUpstream('zero');peer.intent='退款退货';peer.answer_chunks=['按实际政策核查本单条件[1]。']
    with run_server(peer.app) as url,TestClient(create_app(chat_settings(url,context_window_tokens=20000,
            workflow_checkpoint_path=args.checkpoint))) as client:
        service=client.app.state.chat_service
        assert service.settings.mysql_port==3307
        if args.phase=='pause-crash':
            from retailq.workflow.order_steps import OrderSteps
            original=OrderSteps.select_order
            async def spent(self,state):
                self.deadline=time.monotonic()+30
                return await original(self,state)
            OrderSteps.select_order=spent
            def crash(turn,user_id,pending,remaining):
                path.write_text(json.dumps({'cid':turn.conversation_id,'turn_id':turn.turn_id,'remaining':remaining}),encoding='utf-8')
                os._exit(0)
            service.workflow_repository.save_pending=crash
            client.post('/api/chat/stream',json={'user_id':'crash-selector','message':'这个能退吗'})
            raise AssertionError('checkpoint-before-SQL fault did not execute')
        from tests.test_rag_answer import retrieval
        from retailq.knowledge.workflow_policy import workflow_policy_identity
        seed=json.loads(path.read_text(encoding='utf-8'));cid=seed['cid'];turn=seed['turn_id']
        pending=client.get(f'/api/conversations/{cid}/pending?user_id=crash-selector').json()['pending']
        row=service.workflow_repository.get_turn(turn,'crash-selector')
        assert 0<row['payload']['remaining_active_seconds']<=seed['remaining']<=30
        assert 'pause_budget' not in pending and len(peer.requests)==0
        result=retrieval(2)
        policy=path.with_suffix('.policy.json')
        service.settings.workflow_policy_multi_confidence_path=str(policy)
        policy.write_text(json.dumps({'identity':workflow_policy_identity(service.settings,result.snapshot,'policy_multi'),
                                     'calibrated':True,'thresholds':{'policy_multi':0}}),encoding='utf-8')
        service.repository.policy_retrieve=lambda q,*,queries,mode,deadline:SimpleNamespace(**{**vars(result),'strategy':mode})
        events=dict(parse_sse(client.post(f'/api/conversations/{cid}/turns/{turn}/resume',json={
            'user_id':'crash-selector','interrupt_id':pending['interrupt_id'],'request_key':'e'*32,'order_id':'1001'}).text))
        assert 'done' in events and events['meta']['turn_id']==turn
        assert all('[workflow_resolve]' not in r['messages'][0]['content'] and '[workflow_intent]' not in r['messages'][0]['content'] for r in peer.requests)
        path.with_suffix('.resumed.json').write_text(json.dumps({'done':True,'remaining':row['payload']['remaining_active_seconds']}),encoding='utf-8')


if __name__=='__main__':main()
