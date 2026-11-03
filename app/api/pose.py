from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, WebSocket

from app.api.streaming import pump
from app.docs import pose as docs
from app.docs.openapi import TAG_POSE
from app.pose.registry import PoseModelError
from app.schemas.pose import CalibrationOut, PoseLatestOut, PoseModelItem, PoseStatusOut, PoseSwitchIn
from app.services.exceptions import ServiceError
from app.services.pose_live import PoseModelNotFound, PoseService

router = APIRouter(prefix="/api/pose", tags=[TAG_POSE])
ws_router = APIRouter(tags=[TAG_POSE])


def _service(request: Request) -> PoseService:
    service = getattr(request.app.state, "pose", None)
    if service is None:
        raise ServiceError(503, "pose_unavailable", "6대 자세 모델이 꺼져 있습니다. 서버 설정과 모델 파일을 확인해 주세요.")
    return service


@router.get("/status", response_model=PoseStatusOut, **docs.STATUS)
def pose_status(request: Request) -> PoseStatusOut:
    return PoseStatusOut(**_service(request).status())


@router.get("/latest", response_model=PoseLatestOut, **docs.LATEST)
def pose_latest(request: Request) -> PoseLatestOut:
    return PoseLatestOut(latest=_service(request).latest)


@router.get("/models", response_model=list[PoseModelItem], **docs.MODELS)
def pose_models(request: Request) -> list[PoseModelItem]:
    return [PoseModelItem(**m) for m in _service(request).models()]


@router.put("/models/active", response_model=dict, **docs.SWITCH)
def switch_model(request: Request, body: PoseSwitchIn) -> dict:
    service = _service(request)
    try:
        return service.switch(body.name)
    except PoseModelNotFound:
        raise ServiceError(404, "pose_model_not_found", "그 이름의 자세 모델이 없습니다.", {"name": body.name})
    except (PoseModelError, FileNotFoundError, RuntimeError) as exc:
        raise ServiceError(409, "pose_model_invalid", "모델 파일 검사에 실패했습니다. 기존 모델을 계속 사용합니다.",
                           {"name": body.name, "reason": str(exc)})


@router.post("/calibration", response_model=CalibrationOut, **docs.CALIBRATE)
def recalibrate(request: Request) -> CalibrationOut:
    return CalibrationOut(**_service(request).recalibrate())


@ws_router.websocket("/ws/pose")
async def pose_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    service: PoseService | None = getattr(websocket.app.state, "pose", None)
    if service is None:
        await websocket.close(code=4503, reason="pose model unavailable")
        return
    sub = service.subscribe(asyncio.get_running_loop())
    snapshot = await asyncio.to_thread(service.snapshot)
    await pump(websocket, sub, snapshot, "closed", lambda: service.unsubscribe(sub))
