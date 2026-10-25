from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
import asyncio

import anyio
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from retailq.api.chat import router as chat_router
from retailq.db import Database
from retailq.repositories import ChatRepository
from retailq.services.chat import ChatService
from retailq.config import Settings
from retailq.context import prepare_extraction
from retailq.errors import ServiceError
from retailq.gateway import ModelGateway, OpenAIModelGateway
from retailq.schemas import AfterSalesResult, ExtractRequest
from retailq.knowledge.runtime import KnowledgeRuntime
from retailq.knowledge.scheduler import DailyKnowledgeScheduler


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
        gateway=database=knowledge=scheduler=service=None
        try:
            gateway = OpenAIModelGateway(settings) if settings.is_ready else None
            application.state.gateway = gateway
            database = Database(settings) if settings.is_database_configured else None
            if database is not None and settings.is_knowledge_configured:
                with anyio.CancelScope(shield=True):
                    knowledge=await asyncio.to_thread(KnowledgeRuntime.open,settings,database)
                if knowledge.pipeline is not None:
                    scheduler=DailyKnowledgeScheduler(knowledge)
            application.state.knowledge_runtime=knowledge
            application.state.knowledge_scheduler=scheduler
            service = ChatService(ChatRepository(database,faq_search=knowledge.search_faq if knowledge else None),gateway,settings) if database and gateway else None
            application.state.chat_service = service
            if scheduler is not None:scheduler.start()
            yield
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    if service is not None:
                        with anyio.move_on_after(15,shield=True):await service.aclose()
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
    web_directory = Path(__file__).parent / "web"
    application.mount("/assets", StaticFiles(directory=web_directory), name="chat-assets")

    @application.get("/", response_class=FileResponse, include_in_schema=False)
    async def chat_page():
        return FileResponse(web_directory / "index.html", headers={"Cache-Control": "no-cache"})

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
