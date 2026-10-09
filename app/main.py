from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import analyses, events, files, replays, sessions, system
from app.config import Settings, get_settings
from app.docs.openapi import COMMON_RESPONSES, SWAGGER_UI, TAGS, TITLE
from app.errors import install_error_handlers
from app.repositories import build_repositories
from app.services.analysis import AnalysisService
from app.services.events import EventService
from app.services.files import FileService
from app.services.model_guard import verify_model
from app.services.monitoring import SessionManager, run_ticker
from app.services.playback import ReplayManager, run_replay_ticker
from app.services.udp_ingest import UdpCollector, run_udp_flusher, start_udp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("carewave")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.model_info = verify_model(settings.model_path, settings.model_sha256)
        app.state.repos = build_repositories(settings)
        app.state.sessions = SessionManager(settings, app.state.repos, app.state.model_info.sha256)
        app.state.events = EventService(app.state.repos, app.state.sessions, burst_sec=settings.event_burst_sec)
        app.state.files = FileService(settings, app.state.repos)
        app.state.analyses = AnalysisService(settings, app.state.repos, app.state.files)
        app.state.replays = ReplayManager(settings, app.state.analyses)
        app.state.udp = UdpCollector(settings, app.state.sessions)
        for recover in (app.state.files.recover, app.state.analyses.recover):
            try:
                await asyncio.to_thread(recover)
            except Exception:
                log.exception("startup recovery step failed")
        stop = asyncio.Event()
        udp_transport = await start_udp(app.state.udp)
        tickers = [asyncio.create_task(run_ticker(app.state.sessions, stop)),
                   asyncio.create_task(run_replay_ticker(app.state.replays, stop)),
                   asyncio.create_task(run_udp_flusher(app.state.udp, stop))]
        try:
            yield
        finally:
            stop.set()
            await asyncio.gather(*tickers)
            if udp_transport is not None:
                udp_transport.close()
            await asyncio.to_thread(app.state.udp.flush)
            await asyncio.to_thread(app.state.sessions.shutdown)
            app.state.analyses.shutdown()

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
    app.include_router(events.router)
    app.include_router(files.router)
    app.include_router(analyses.router)
    app.include_router(replays.router)
    app.include_router(replays.ws_router)
    return app


app = create_app()
