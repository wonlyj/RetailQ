"""Chat-completions peer that chooses its next step from actual tool receipts."""
import json
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


class WorkflowUpstream:
    def __init__(self, scenario='two', *, final_reason='stop', bad_actions=None):
        self.scenario, self.final_reason, self.bad_actions = scenario, final_reason, bad_actions
        self.requests = []
        self.intent = '物流'
        self.answer_chunks=['订单1001：','以下为演示数据。']
        self.require_decision_stage=False
        self.resolved=None
        self.core_case=True
        self.app = FastAPI()
        self.app.post('/chat/completions')(self.complete)
        self.app.post('/v1/chat/completions')(self.complete)

    async def complete(self, request: Request):
        body = await request.json()
        self.requests.append(body)
        messages = body['messages']
        marker=messages[0].get('content','')
        if any(x in marker for x in ('[workflow_resolve]','[workflow_query_plan]','[workflow_expand]','[workflow_intent]')):
            if '[workflow_resolve]' in marker:
                value={'resolved_question':self.resolved or next(m['content'] for m in reversed(messages) if m['role']=='user')}
            elif '[workflow_query_plan]' in marker: value={'core_case':self.core_case}
            elif '[workflow_expand]' in marker: value={'queries':['商品退货期限和适用条件','商品退货例外及申请材料']}
            else:value={'intent':self.intent,'confidence':0.95}
            return JSONResponse({'id':'intent','object':'chat.completion','model':'test',
                'choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(value,ensure_ascii=False)},
                            'finish_reason':'stop'}],
                'usage':{'prompt_tokens':50,'completion_tokens':15,'total_tokens':65}})
        if body.get('stream'):
            async def frames():
                for text in self.answer_chunks:
                    yield 'data: ' + json.dumps({'id':'wf','object':'chat.completion.chunk','model':'test',
                        'choices':[{'index':0,'delta':{'content':text},'finish_reason':None}]}) + '\n\n'
                yield 'data: ' + json.dumps({'id':'wf','object':'chat.completion.chunk','model':'test',
                    'choices':[{'index':0,'delta':{},'finish_reason':self.final_reason}]}) + '\n\n'
                yield 'data: [DONE]\n\n'
            return StreamingResponse(frames(), media_type='text/event-stream')
        tools = [m for m in messages if m['role'] == 'tool']
        requests = {c['id']:c for m in messages for c in m.get('tool_calls',[])}
        last_name = requests[tools[-1]['tool_call_id']]['function']['name'] if tools else None
        name = None
        if self.scenario == 'two':
            if not tools: name = 'query_order'
            elif last_name == 'query_order':
                assert json.loads(tools[-1]['content'])['data']['order_id'] == '1001'
                name = 'query_logistics'
        elif self.scenario == 'one' and not tools: name = 'query_logistics'
        elif self.scenario in ('create_ticket', 'query_faq', 'repeat', 'parallel'):
            name = {'repeat':'query_order','parallel':'query_order'}.get(self.scenario,self.scenario)
        call = {'id':f'call_{len(tools)}','type':'function','function':{'name':name,
                'arguments':json.dumps({'order_id':'1001'})}}
        calls = ([call, {**call,'id':'second'}] if self.scenario == 'parallel' else [call]) if name else []
        content = '' if calls else (self.bad_actions if self.bad_actions is not None else '{"actions":[]}')
        if not calls and self.require_decision_stage and '[workflow_decision]' not in messages[-1].get('content',''):
            content += '\n\n请提供订单号。'
        return JSONResponse({'id':'wf','object':'chat.completion','model':'test',
            'choices':[{'index':0,'message':{'role':'assistant','content':content,'tool_calls':calls},
                        'finish_reason':'tool_calls' if calls else 'stop'}],
            'usage':{'prompt_tokens':50,'completion_tokens':15,'total_tokens':65}})
