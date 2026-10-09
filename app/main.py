from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import system
from app.config import Settings, get_settings
from app.errors import install_error_handlers
from app.repositories import build_repositories
from app.schemas.system import ErrorOut
from app.services.model_guard import verify_model

DESCRIPTION = """
Wi-Fi CSI로 야간 움직임을 감지하는 CareWave 대시보드 백엔드입니다.

- 판정 모델: `lying_walking_v2` (서버 시작 시 파일 해시 검사)
- 시각 값(`*_ts`)은 UNIX 초(UTC)입니다.
- 모든 오류는 `{"error": {"code", "message", "detail"}}` 형식으로 응답합니다.
"""

TAGS = [
    {"name": "시스템", "description": "서버 상태와 모델 정보"},
]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.model_info = verify_model(settings.model_path, settings.model_sha256)
        app.state.repos = build_repositories(settings)
        yield

    app = FastAPI(
        title="CareWave Motion State API",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        responses={
            422: {"model": ErrorOut, "description": "요청 또는 파일 형식 오류"},
            503: {"model": ErrorOut, "description": "DB 또는 Storage 연결 실패"},
        },
        swagger_ui_parameters={"defaultModelsExpandDepth": 0, "displayRequestDuration": True},
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
    return app


app = create_app()
