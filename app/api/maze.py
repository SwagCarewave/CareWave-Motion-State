from __future__ import annotations

from fastapi import APIRouter, Query, Request, status

from app.docs import maze as docs
from app.docs.openapi import TAG_MAZE
from app.schemas.maze import MazeRankingOut, MazeRecordIn, MazeRecordOut
from app.services.maze import MazeService

router = APIRouter(prefix="/api/maze", tags=[TAG_MAZE])


def _service(request: Request) -> MazeService:
    return request.app.state.maze


@router.post("/records", response_model=MazeRecordOut, status_code=status.HTTP_201_CREATED, **docs.CREATE)
def create_record(request: Request, body: MazeRecordIn) -> MazeRecordOut:
    return MazeRecordOut(**_service(request).create(body.name, body.minutes, body.seconds))


@router.get("/records", response_model=MazeRankingOut, **docs.RANKING)
def ranking(request: Request,
            limit: int | None = Query(default=None, ge=1, description=docs.LIMIT_PARAM)) -> MazeRankingOut:
    return MazeRankingOut(**_service(request).ranking(limit))
