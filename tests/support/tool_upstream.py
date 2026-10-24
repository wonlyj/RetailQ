import asyncio
import json
import threading

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


def wire_call(name='query_logistics', args=None, call_id='call_1'):
    return {'id':call_id,'type':'function','function':{'name':name,'arguments':
        json.dumps({'order_id':'1001'} if args is None else args,ensure_ascii=False)}}


class ControlledToolUpstream:
    def __init__(self, calls=None, *, no_tool=False, selection_reason='tool_calls',
                 chunks=None, final_reason='stop', terminal=True, final_tool=False, gated=False):
        self.calls = [] if no_tool else ([wire_call()] if calls is None else calls)
        self.selection_reason = 'stop' if no_tool else selection_reason
        self.chunks = ['演示物流：','订单1001正在配送。'] if chunks is None else chunks
        self.requests = []
        self.release = threading.Event()
        self.closed = threading.Event()
        self.finished = threading.Event()
        self.app = FastAPI()

        @self.app.post('/v1/chat/completions')
        async def chat(request: Request):
            body = await request.json()
            self.requests.append((body,dict(request.headers)))
            if not body.get('stream'):
                message = {'role':'assistant','content':'' if self.calls else '无需查询。'}
                if self.calls:
                    message['tool_calls'] = self.calls
                return {'id':'test-plan','object':'chat.completion','created':0,'model':'test-model',
                        'choices':[{'index':0,'message':message,'finish_reason':self.selection_reason}]}

            def event(delta, reason=None):
                return 'data: '+json.dumps({'id':'test-final','object':'chat.completion.chunk','created':0,
                    'model':'test-model','choices':[{'index':0,'delta':delta,'finish_reason':reason}]})+'\n\n'

            async def stream():
                try:
                    for index, chunk in enumerate(self.chunks):
                        if index and gated:
                            while not self.release.is_set():
                                await asyncio.sleep(0.01)
                        delta = {'content':chunk}
                        if final_tool:
                            delta = {'tool_calls':[dict(index=0,**wire_call())]}
                        yield event(delta)
                    if terminal:
                        yield event({},final_reason)
                        yield 'data: [DONE]\n\n'
                    self.finished.set()
                finally:
                    self.closed.set()
            return StreamingResponse(stream(),media_type='text/event-stream')
