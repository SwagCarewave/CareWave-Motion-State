from __future__ import annotations

import asyncio

from fastapi import APIRouter, Path, Request, WebSocket, status

from app.api.streaming import pump
from app.docs import replays as docs
from app.docs.openapi import TAG_CSV
from app.schemas.replays import ReplayCreateIn, ReplayOut, ReplayUpdateIn
from app.services.exceptions import ServiceError
from app.services.playback import ReplayManager

router = APIRouter(prefix="/api/replays", tags=[TAG_CSV])
ws_router = APIRouter(tags=[TAG_CSV])


def _replays(request: Request) -> ReplayManager:
    return request.app.state.replays


@router.post("", response_model=ReplayOut, status_code=status.HTTP_201_CREATED, **docs.CREATE)
def create_replay(request: Request, body: ReplayCreateIn) -> ReplayOut:
    return ReplayOut(**_replays(request).create(body.analysis_id, body.from_ts, body.to_ts, body.speed,
                                                body.position_ts, body.play))


@router.get("/{replay_id}", response_model=ReplayOut, **docs.GET)
def get_replay(request: Request, replay_id: str = Path(description=docs.REPLAY_ID_PARAM)) -> ReplayOut:
    return ReplayOut(**_replays(request).state(replay_id))


@router.patch("/{replay_id}", response_model=ReplayOut, **docs.UPDATE)
def update_replay(request: Request, body: ReplayUpdateIn,
                  replay_id: str = Path(description=docs.REPLAY_ID_PARAM)) -> ReplayOut:
    return ReplayOut(**_replays(request).update(replay_id, body.action, body.position_ts, body.speed,
                                                body.from_ts, body.to_ts))


@ws_router.websocket("/ws/replays/{replay_id}")
async def replay_stream(websocket: WebSocket, replay_id: str) -> None:
    manager: ReplayManager = websocket.app.state.replays
    await websocket.accept()
    try:
        sub = await asyncio.to_thread(manager.subscribe, replay_id, asyncio.get_running_loop())
        snapshot = await asyncio.to_thread(manager.snapshot, replay_id)
    except ServiceError:
        await websocket.close(code=4404, reason="replay not found")
        return
    await pump(websocket, sub, snapshot, "removed", lambda: manager.unsubscribe(replay_id, sub))
