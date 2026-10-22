import asyncio
import json
import threading

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


class ControlledUpstream:
    def __init__(self, *, status=200, text=None, chunks=None, gated=False, finish_reason="stop", tool=False, nonstream_delay=0):
        self.status = status
        self.text = text if text is not None else json.dumps({
            "order_id": "00123", "request_type": "exchange", "expected_solution": "换成42码",
        }, ensure_ascii=False)
        self.chunks = ["您好", "，请问有什么购物问题？"] if chunks is None else chunks
        self.gated = gated
        self.finish_reason = finish_reason
        self.tool = tool
        self.nonstream_delay = nonstream_delay
        self.requests = []
        self.release = threading.Event()
        self.finished = threading.Event()
        self.closed = threading.Event()
        self.app = FastAPI()

        @self.app.post("/v1/chat/completions")
        async def chat(request: Request):
            body = await request.json()
            self.requests.append((body, dict(request.headers)))
            if self.status != 200:
                return JSONResponse({"error": {"message": "sensitive-upstream-secret", "type": "test_error"}}, status_code=self.status)
            if not body.get("stream"):
                if self.nonstream_delay:
                    await asyncio.sleep(self.nonstream_delay)
                return {"id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "test-model",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": self.text}, "finish_reason": "stop"}]}

            async def stream():
                def event(delta, reason=None):
                    return "data: " + json.dumps({"id": "chatcmpl-test", "object": "chat.completion.chunk",
                        "created": 0, "model": "test-model", "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}) + "\n\n"
                try:
                    for i, chunk in enumerate(self.chunks):
                        if i and self.gated:
                            while not self.release.is_set():
                                await asyncio.sleep(0.01)
                        if self.tool:
                            yield event({"tool_calls": [{"index": 0, "id": "call-test", "type": "function",
                                          "function": {"name": "forbidden", "arguments": "{}"}}]})
                        else:
                            yield event({"content": chunk})
                    yield event({}, self.finish_reason)
                    yield "data: [DONE]\n\n"
                    self.finished.set()
                finally:
                    self.closed.set()
            return StreamingResponse(stream(), media_type="text/event-stream")

    @property
    def request_count(self):
        return len(self.requests)
