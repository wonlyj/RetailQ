from fastapi import FastAPI
from pydantic import ValidationError

from retailq.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    try:
        settings = settings if settings is not None else Settings()
    except ValidationError:
        raise RuntimeError("应用配置无效，请检查 .env 配置。") from None
    application = FastAPI(title="RetailQ", version="0.1.0")
    application.state.settings = settings

    @application.get("/health")
    async def health():
        return {"status": "ok", "model_configured": settings.is_ready}

    return application


app = create_app()
