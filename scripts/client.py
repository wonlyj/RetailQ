"""Small API client shared by this chapter's smoke and evaluation commands."""
import time

import httpx


class ApiFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def check_response(response: httpx.Response) -> None:
    if response.status_code != 200:
        try:
            code = response.json().get('error',{}).get('code')
        except (ValueError,AttributeError,TypeError):
            code = None
        raise ApiFailure(code if isinstance(code,str) else f"api_http_{response.status_code}")


async def chat(client: httpx.AsyncClient, base_url: str, message: str, history: list, *,
               conversation_id: int | None = None, user_id: str = 'local-demo') -> dict:
    import json

    start = time.perf_counter()
    text = []
    first = None
    terminal = None
    metadata = {}
    tool_status = []
    body = {'message':message,'history':history,'user_id':user_id}
    if conversation_id is not None:
        body['conversation_id'] = conversation_id
    async with client.stream("POST", base_url.rstrip("/") + "/api/chat/stream",
                             json=body) as response:
        if response.status_code != 200:
            await response.aread()
        check_response(response)
        event = None
        data = []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line:
                if data:
                    try:
                        payload = json.loads("\n".join(data))
                    except (ValueError, TypeError):
                        raise ApiFailure("invalid_sse_json") from None
                    if event == "error":
                        raise ApiFailure(payload.get('code','stream_error'))
                    if terminal is not None:
                        raise ApiFailure("events_after_done")
                    if event == "token":
                        value = payload.get("text")
                        if not isinstance(value, str) or not value:
                            raise ApiFailure("invalid_token_event")
                        if first is None:
                            first = time.perf_counter() - start
                        text.append(value)
                    elif event == "meta":
                        metadata = payload
                    elif event == 'tool_status':
                        if (not isinstance(payload.get('name'),str) or not isinstance(payload.get('tool_call_id'),str)
                                or payload.get('status') not in ('running','completed','failed')):
                            raise ApiFailure('invalid_tool_status')
                        tool_status.append(payload)
                    elif event == "done":
                        terminal = payload.get("finish_reason")
                    else:
                        raise ApiFailure("unexpected_sse_event")
                event, data = None, []
    if not text or terminal != "stop":
        raise ApiFailure("incomplete_or_truncated_stream")
    return {"text": "".join(text), "token_events": len(text), "first_token_seconds": first,
            "total_seconds": time.perf_counter() - start, "finish_reason": terminal, "meta": metadata,
            'conversation_id':metadata.get('conversation_id'),'tool_status':tool_status}


async def extract(client: httpx.AsyncClient, base_url: str, description: str) -> dict:
    from retailq.schemas import AfterSalesResult

    response = await client.post(base_url.rstrip("/") + "/api/after-sales/extract", json={"description": description})
    check_response(response)
    return AfterSalesResult.model_validate(response.json()).model_dump()
