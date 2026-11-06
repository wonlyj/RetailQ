"""Small API client shared by this chapter's smoke and evaluation commands."""
import time
import re

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
    action_envelope=None;source_envelope=None;message_id=None
    def bind_source(payload):
        nonlocal message_id
        if (payload.get('turn_id')!=metadata.get('turn_id') or
            not re.fullmatch(r'[1-9]\d*',str(payload.get('message_id',''))) or
            (message_id is not None and message_id!=payload['message_id'])):
            raise ApiFailure('workflow_source_mismatch')
        message_id=payload['message_id']
    tool_status = []
    workflow, actions, sources = [], [], []
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
                        if metadata:raise ApiFailure('duplicate_meta')
                        if payload.get('turn_id') is not None and (not re.fullmatch(r'[0-9a-f]{32}',str(payload['turn_id'])) or
                            type(payload.get('conversation_id')) is not int or payload['conversation_id']<1):raise ApiFailure('invalid_workflow_meta')
                        metadata = payload
                    elif event == 'tool_status':
                        if (not isinstance(payload.get('name'),str) or not isinstance(payload.get('tool_call_id'),str)
                                or payload.get('status') not in ('running','completed','failed')):
                            raise ApiFailure('invalid_tool_status')
                        tool_status.append(payload)
                    elif event == 'workflow':
                        if (not isinstance(payload.get('node'),str) or payload.get('phase') not in ('enter','exit') or
                            not metadata.get('turn_id') or payload.get('turn_id')!=metadata['turn_id']):
                            raise ApiFailure('invalid_workflow_event')
                        workflow.append(payload)
                    elif event == 'actions':
                        bind_source(payload)
                        items=payload.get('actions')
                        if (action_envelope is not None or payload.get('conversation_id')!=metadata.get('conversation_id') or
                                not isinstance(payload.get('message_id'),str) or not payload['message_id'].isdigit()
                                or not isinstance(items,list) or len(items)>2 or
                                any(not isinstance(a,dict) or a.get('kind') not in ('handoff','ticket') or
                                    not re.fullmatch(r'[0-9a-f]{32}',str(a.get('action_id',''))) or
                                    a.get('origin_assistant_message_id')!=payload['message_id'] or
                                    a.get('status') not in ('offered','executing','unknown','completed','failed') for a in items) or
                                len({a['kind'] for a in items})!=len(items)):
                            raise ApiFailure('invalid_actions_event')
                        actions=items;action_envelope=payload
                    elif event == 'sources':
                        from retailq.api.source_schemas import PublicCitation
                        try:
                            if metadata.get('turn_id'):
                                if source_envelope is not None:raise ValueError()
                                bind_source(payload)
                            if not isinstance(payload.get('citations'),list):raise ValueError()
                            sources=[PublicCitation.model_validate(c).model_dump() for c in payload['citations']]
                            if not isinstance(payload.get('message_id'),str):raise ValueError()
                            source_envelope=payload
                        except (TypeError,ValueError):raise ApiFailure('invalid_sources_event') from None
                    elif event == "done":
                        terminal = payload.get("finish_reason")
                    else:
                        raise ApiFailure("unexpected_sse_event")
                event, data = None, []
    if not text or terminal != "stop":
        raise ApiFailure("incomplete_or_truncated_stream")
    if metadata.get('turn_id') and action_envelope is None:raise ApiFailure('missing_actions_event')
    return {"text": "".join(text), "token_events": len(text), "first_token_seconds": first,
            "total_seconds": time.perf_counter() - start, "finish_reason": terminal, "meta": metadata,
            'conversation_id':metadata.get('conversation_id'),'tool_status':tool_status,
            'workflow':workflow,'actions':actions,'sources':sources,
            'action_envelope':action_envelope,'source_envelope':source_envelope}


async def extract(client: httpx.AsyncClient, base_url: str, description: str) -> dict:
    from retailq.schemas import AfterSalesResult

    response = await client.post(base_url.rstrip("/") + "/api/after-sales/extract", json={"description": description})
    check_response(response)
    return AfterSalesResult.model_validate(response.json()).model_dump()
