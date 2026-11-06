from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
import asyncio
import sqlite3

import anyio
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from retailq.api.chat import router as chat_router
from retailq.api.knowledge import router as knowledge_router
from retailq.api.sources import router as sources_router
from retailq.api.actions import router as actions_router
from retailq.workflow.actions import TicketActionService
from retailq.api.knowledge_access import require_console_local
from retailq.db import Database
from retailq.repositories import ChatRepository
from retailq.workflow.service import WorkflowChatService
from retailq.workflow.runtime import WorkflowRuntime
from retailq.workflow.repository import WorkflowRepository
from retailq.coordination import Coordinator
from retailq.config import Settings
from retailq.context import prepare_extraction
from retailq.errors import ServiceError
from retailq.gateway import ModelGateway, OpenAIModelGateway
from retailq.schemas import AfterSalesResult, ExtractRequest
from retailq.knowledge.runtime import KnowledgeRuntime
from retailq.knowledge.scheduler import DailyKnowledgeScheduler
from retailq.knowledge.review_jobs import ReviewJobs


def get_gateway(request: Request) -> ModelGateway:
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is None:
        raise ServiceError("model_not_configured", 503, "请先在本地 .env 中配置模型服务。")
    return gateway


def create_app(settings: Settings | None = None) -> FastAPI:
    try:
        settings = settings if settings is not None else Settings()
    except ValidationError:
        raise RuntimeError("应用配置无效，请检查 .env 配置。") from None
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        gateway=database=knowledge=scheduler=service=jobs=workflow_context=action_service=None
        application.state.knowledge_failure=None
        application.state.workflow_failure=None
        application.state.workflow_repository=None
        application.state.ticket_actions=None
        try:
            gateway = OpenAIModelGateway(settings) if settings.is_ready else None
            application.state.gateway = gateway
            database = Database(settings) if settings.is_database_configured else None
            if database is not None and settings.is_knowledge_configured:
                try:
                    with anyio.CancelScope(shield=True):
                        knowledge=await asyncio.to_thread(KnowledgeRuntime.open,settings,database)
                except ServiceError as exc:
                    # open() owns and releases partially initialized resources.
                    # Keep the local page available to explain its safe failure;
                    # no GET retries initialization and FAQ remains unavailable.
                    application.state.knowledge_failure=exc
                else:
                    jobs=ReviewJobs(knowledge)
                    if knowledge.can_prepare_review:
                        scheduler=DailyKnowledgeScheduler(knowledge,jobs=jobs)
            application.state.knowledge_runtime=knowledge
            application.state.knowledge_scheduler=scheduler
            application.state.review_jobs=jobs
            if database and gateway:
                repository=ChatRepository(database,faq_search=knowledge.search_faq if knowledge else None)
                repository.rag_required=settings.knowledge_enabled
                if knowledge is not None:
                    repository.rag_retrieve=knowledge.retrieve
                from retailq.knowledge.policy import DynamicConfidencePolicy
                workflow_repository=WorkflowRepository(database)
                application.state.workflow_repository=workflow_repository
                workflow_context=WorkflowRuntime.open(settings,workflow_repository,repository,gateway,knowledge,
                    DynamicConfidencePolicy(settings) if knowledge else None,
                    knowledge.coordinator if knowledge else Coordinator.for_settings(settings,bind_knowledge=False))
                try:
                    runtime=await workflow_context.__aenter__()
                    service=WorkflowChatService(runtime)
                    action_service=TicketActionService(workflow_repository,repository,runtime.coordinator,settings)
                    application.state.ticket_actions=action_service
                except ServiceError as exc:
                    application.state.workflow_failure=exc
                    workflow_context=None
                except (OSError,sqlite3.Error):
                    application.state.workflow_failure=ServiceError('workflow_checkpoint_unavailable',503,'检查点文件不可用，请检查路径和权限。')
                    workflow_context=None
            application.state.chat_service = service
            if scheduler is not None:scheduler.start()
            yield
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    if action_service is not None:await action_service.aclose()
                    if service is not None:
                        await service.aclose()
                finally:
                    try:
                        if workflow_context is not None:await workflow_context.__aexit__(None,None,None)
                    finally:
                        try:
                            if jobs is not None:await jobs.aclose()
                        finally:
                            try:
                                if scheduler is not None:await scheduler.stop()
                            finally:
                                try:
                                    if knowledge is not None:await asyncio.to_thread(knowledge.close)
                                finally:
                                    try:
                                        if database is not None:database.close()
                                    finally:
                                        if gateway is not None:
                                            with anyio.move_on_after(5,shield=True):await gateway.aclose()

    application = FastAPI(title="RetailQ", version="0.3.0", lifespan=lifespan)
    application.state.settings = settings
    application.include_router(chat_router)
    application.include_router(knowledge_router)
    application.include_router(sources_router)
    application.include_router(actions_router)
    web_directory = Path(__file__).parent / "web"
    application.mount("/assets", StaticFiles(directory=web_directory), name="chat-assets")

    @application.get("/", response_class=FileResponse, include_in_schema=False)
    async def chat_page():
        return FileResponse(web_directory / "index.html", headers={"Cache-Control": "no-cache"})

    @application.get('/knowledge', response_class=FileResponse, include_in_schema=False)
    async def knowledge_page(request: Request):
        require_console_local(request)
        return FileResponse(web_directory / 'knowledge.html', headers={'Cache-Control':'no-cache'})

    @application.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.public_message}}, status_code=exc.status_code)

    @application.get("/health")
    async def health():
        return {"status":"ok","model_configured":settings.is_ready,"database_configured":settings.is_database_configured}

    @application.post("/api/after-sales/extract", response_model=AfterSalesResult)
    async def extract_after_sales(
        body: ExtractRequest, request: Request,
        gateway: Annotated[ModelGateway, Depends(get_gateway)],
    ) -> AfterSalesResult:
        messages = prepare_extraction(body.description, request.app.state.settings)
        return await gateway.extract(messages)

    return application


app = create_app()
