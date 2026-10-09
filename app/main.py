from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import sessions, system
from app.config import Settings, get_settings
from app.docs.openapi import COMMON_RESPONSES, SWAGGER_UI, TAGS, TITLE
from app.errors import install_error_handlers
from app.repositories import build_repositories
from app.services.model_guard import verify_model
from app.services.monitoring import SessionManager, run_ticker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.model_info = verify_model(settings.model_path, settings.model_sha256)
        app.state.repos = build_repositories(settings)
        app.state.sessions = SessionManager(settings, app.state.repos, app.state.model_info.sha256)
        stop = asyncio.Event()
        ticker = asyncio.create_task(run_ticker(app.state.sessions, stop))
        try:
            yield
        finally:
            stop.set()
            await ticker
            await asyncio.to_thread(app.state.sessions.shutdown)

    app = FastAPI(
        title=TITLE,
        version=__version__,
        openapi_tags=TAGS,
        responses=COMMON_RESPONSES,
        swagger_ui_parameters=SWAGGER_UI,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials="*" not in settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_error_handlers(app)
    app.include_router(system.router)
    app.include_router(sessions.router)
    app.include_router(sessions.ws_router)
    return app


app = create_app()
