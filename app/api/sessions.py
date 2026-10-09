from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request, Response, WebSocket, status

from app.api.streaming import pump
from app.docs import sessions as docs
from app.docs.openapi import TAG_LIVE
from app.schemas.codes import SessionStatus
from app.schemas.sessions import IngestOut, PacketBatchIn, SessionOut, SessionStartOut, SignalsOut
from app.services.monitoring import SessionManager, SessionNotFound, SessionStopped

router = APIRouter(prefix="/api/sessions", tags=[TAG_LIVE])
ws_router = APIRouter(tags=[TAG_LIVE])


def _manager(request: Request) -> SessionManager:
    return request.app.state.sessions


@router.post("", response_model=SessionStartOut, status_code=status.HTTP_201_CREATED, **docs.START)
def start_session(request: Request, response: Response) -> SessionStartOut:
    manager = _manager(request)
    row, created = manager.start()
    if not created:
        response.status_code = status.HTTP_200_OK
    return SessionStartOut(**manager.status(row["id"]), created=created)


@router.get("", response_model=list[SessionOut], **docs.LIST)
def list_sessions(request: Request,
                  status_filter: SessionStatus | None = Query(default=None, alias="status", description=docs.STATUS_PARAM),
                  limit: int = Query(default=20, ge=1, le=100, description=docs.LIMIT_PARAM)) -> list[SessionOut]:
    manager = _manager(request)
    return [SessionOut(**manager.status(r["id"])) for r in manager.list(status_filter, limit)]


@router.get("/{session_id}", response_model=SessionOut, **docs.GET)
def get_session(request: Request, session_id: str) -> SessionOut:
    return SessionOut(**_manager(request).status(session_id))


@router.post("/{session_id}/stop", response_model=SessionOut, **docs.STOP)
def stop_session(request: Request, session_id: str) -> SessionOut:
    manager = _manager(request)
    manager.stop(session_id)
    return SessionOut(**manager.status(session_id))


@router.post("/{session_id}/packets", response_model=IngestOut, **docs.PACKETS)
def ingest_packets(request: Request, session_id: str, body: PacketBatchIn) -> IngestOut:
    result = _manager(request).ingest(session_id, [p.model_dump() for p in body.packets])
    return IngestOut(**result.to_dict())


@router.get("/{session_id}/signals", response_model=SignalsOut, **docs.SIGNALS)
def get_signals(request: Request, session_id: str,
                window: int | None = Query(default=600, ge=10, le=3600, description=docs.WINDOW_PARAM),
                from_ts: float | None = Query(default=None, alias="from", description=docs.FROM_PARAM),
                to_ts: float | None = Query(default=None, alias="to", description=docs.TO_PARAM),
                max_points: int = Query(default=1200, ge=10, le=7200, description=docs.MAX_POINTS_PARAM),
                heatmap: bool = Query(default=True, description=docs.HEATMAP_PARAM)) -> SignalsOut:
    data = _manager(request).signals(session_id, window_sec=window, from_ts=from_ts, to_ts=to_ts,
                                     max_points=max_points, heatmap=heatmap)
    return SignalsOut(**data)


@ws_router.websocket("/ws/sessions/{session_id}")
async def session_stream(websocket: WebSocket, session_id: str) -> None:
    manager: SessionManager = websocket.app.state.sessions
    await websocket.accept()
    try:
        sub = await asyncio.to_thread(manager.subscribe, session_id, asyncio.get_running_loop())
        snapshot = await asyncio.to_thread(manager.snapshot, session_id)
    except SessionNotFound:
        await websocket.close(code=4404, reason="session not found")
        return
    except SessionStopped:
        await websocket.close(code=4409, reason="session stopped")
        return
    await pump(websocket, sub, snapshot, "stopped", lambda: manager.unsubscribe(session_id, sub))
