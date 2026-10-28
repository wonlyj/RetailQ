"""Thin local management API: read projections or submit durable commands."""
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse

from retailq.api.knowledge_access import ConsoleRoute
from retailq.api.knowledge_schemas import PrepareBody, DecisionBody, RequestKeyBody, SealBody, console_json, db_id
from retailq.errors import ServiceError
from retailq.knowledge.review_contracts import (
    DecisionCommand, PrepareCommand, ResumeCommand, RetryVectorsCommand, SealCommand,
)

router = APIRouter(prefix='/api/knowledge', route_class=ConsoleRoute)
RunID = Annotated[str, Path(pattern=r'^[0-9a-f]{32}$')]
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]
Search = Annotated[str, Query(max_length=200)]


def runtime(request):
    instance = getattr(request.app.state, 'knowledge_runtime', None)
    if instance is None:
        raise ServiceError('knowledge_not_configured',503,'知识库未就绪，请检查本地知识功能配置与启动结果。')
    return instance


def result(view):
    return JSONResponse(console_json(view), headers={'Cache-Control':'no-store'})


@router.get('/summary')
def summary(request: Request):
    return result(runtime(request).review_queries.summary())


@router.get('/sources')
def sources(request: Request, query: Search='', eligibility: Literal['all','eligible','ineligible']='all',
            offset: Offset=0, limit: Limit=20):
    return result(runtime(request).review_queries.sources(query=query,eligibility=eligibility,offset=offset,limit=limit))


@router.get('/sources/{conversation_id}')
def source(request: Request, conversation_id: str):
    return result(runtime(request).review_queries.source(db_id(conversation_id)))


@router.get('/categories')
def categories(request: Request, query: Search='', offset: Offset=0, limit: Limit=20):
    return result(runtime(request).review_queries.categories(query=query,offset=offset,limit=limit))


@router.get('/runs')
def runs(request: Request, offset: Offset=0, limit: Limit=20):
    return result(runtime(request).review_queries.runs(offset=offset,limit=limit))


@router.get('/runs/{run_id}')
def run(request: Request, run_id: RunID):
    return result(runtime(request).review_queries.run(run_id))


@router.get('/runs/{run_id}/groups')
def groups(request: Request, run_id: RunID, query: Search='',
           disposition: Literal['all','new','merge','rejected','pending']='all',
           category: Annotated[str, Query(max_length=255)]='', offset: Offset=0, limit: Limit=20):
    return result(runtime(request).review_queries.groups(run_id,query=query,disposition=disposition,
                                                        category=category,offset=offset,limit=limit))


@router.get('/runs/{run_id}/groups/{group_id}')
def group(request: Request, run_id: RunID, group_id: RunID):
    return result(runtime(request).review_queries.group(run_id,group_id))


@router.get('/chunks/{chunk_id}')
def chunk(request: Request, chunk_id: str):
    return result(runtime(request).review_queries.chunk(db_id(chunk_id)))


@router.get('/operations/{operation_id}')
def operation(request: Request, operation_id: RunID):
    return result(runtime(request).review_queries.operation(operation_id))


@router.get('/legacy-runs/{run_id}')
def legacy(request: Request, run_id: RunID):
    return result(runtime(request).review_queries.legacy_run(run_id))


async def submit(request, command):
    instance = runtime(request)
    try:
        existing = instance.review_store.get_operation(command.request_key)
    except ServiceError as exc:
        if exc.status_code != 404:
            raise
        existing = None
    view = await request.app.state.review_jobs.submit(command)
    status = 200 if existing is not None and view.phase in ('succeeded','failed','sealed') else 202
    return JSONResponse(console_json(view),status_code=status,headers={'Cache-Control':'no-store'})


@router.post('/runs')
async def prepare(request: Request, body: PrepareBody):
    return await submit(request,PrepareCommand(request_key=body.request_key,
        conversation_ids=tuple(db_id(cid) for cid in body.conversation_ids) if body.conversation_ids is not None else None))


@router.post('/runs/{run_id}/resume')
async def resume(request: Request, run_id: RunID, body: RequestKeyBody):
    return await submit(request,ResumeCommand(request_key=body.request_key,run_id=run_id))


@router.post('/runs/{run_id}/retry-vectors')
async def retry_vectors(request: Request, run_id: RunID, body: RequestKeyBody):
    return await submit(request,RetryVectorsCommand(request_key=body.request_key,run_id=run_id))


@router.post('/runs/{run_id}/seal')
async def seal(request: Request, run_id: RunID, body: SealBody):
    if body.confirm_run_id != run_id:
        raise ServiceError('knowledge_seal_confirmation_invalid',422,'请明确确认同一个任务ID。')
    return await submit(request,SealCommand(request_key=body.request_key,run_id=run_id,
                                           confirm_run_id=body.confirm_run_id,reason=body.reason))


@router.post('/runs/{run_id}/groups/{group_id}/decision')
async def decide(request: Request, run_id: RunID, group_id: RunID, body: DecisionBody):
    return await submit(request,DecisionCommand(request_key=body.request_key,run_id=run_id,group_id=group_id,
        expected_revision=body.expected_revision,decision=body.decision,category=body.category,reason=body.reason))
