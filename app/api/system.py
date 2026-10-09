from __future__ import annotations

from fastapi import APIRouter, Request

from app import __version__
from app.schemas.system import HealthOut, ModelOut

router = APIRouter(prefix="/api", tags=["시스템"])


@router.get("/health", response_model=HealthOut, summary="서버 상태 확인",
            description="서버 동작, 최종 모델 해시 검사, DB·Storage 연결 상태를 확인합니다.")
def health(request: Request) -> HealthOut:
    state = request.app.state
    checks = state.repos.ping()
    return HealthOut(
        status="ok" if all(v == "ok" for v in checks.values()) else "degraded",
        model=state.model_info.path.stem,
        model_verified=True,
        storage=state.repos.backend,
        checks=checks,
        version=__version__,
    )


@router.get("/model", response_model=ModelOut, summary="모델 정보 조회",
            description="판정에 사용 중인 최종 모델의 종류, 특징 수, 기준값, 해시를 반환합니다.")
def model(request: Request) -> ModelOut:
    return ModelOut(**request.app.state.model_info.to_dict())
