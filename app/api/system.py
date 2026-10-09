from __future__ import annotations

from fastapi import APIRouter, Request

from app import __version__
from app.docs import system as docs
from app.docs.openapi import TAG_SYSTEM
from app.schemas.codes import catalog
from app.schemas.system import CodesOut, HealthOut, ModelOut

router = APIRouter(prefix="/api", tags=[TAG_SYSTEM])


@router.get("/health", response_model=HealthOut, **docs.HEALTH)
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


@router.get("/model", response_model=ModelOut, **docs.MODEL)
def model(request: Request) -> ModelOut:
    return ModelOut(**request.app.state.model_info.to_dict())


@router.get("/codes", response_model=CodesOut, **docs.CODES)
def codes() -> CodesOut:
    return CodesOut(**catalog())
