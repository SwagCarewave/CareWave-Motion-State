from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Path, Query, Request

from app.docs import events as docs
from app.docs.openapi import TAG_EVENTS
from app.schemas.codes import EventFilter
from app.schemas.events import ConfirmationIn, EventListOut, EventOut
from app.services.events import EventService

router = APIRouter(prefix="/api/events", tags=[TAG_EVENTS])


def _service(request: Request) -> EventService:
    return request.app.state.events


@router.get("", response_model=EventListOut, **docs.LIST)
def list_events(request: Request,
                session_id: str | None = Query(default=None, description=docs.SESSION_PARAM),
                analysis_id: str | None = Query(default=None, description=docs.ANALYSIS_PARAM),
                status: EventFilter = Query(default="all", description=docs.STATUS_PARAM),
                order: Literal["asc", "desc"] = Query(default="asc", description=docs.ORDER_PARAM),
                limit: int = Query(default=50, ge=1, le=500, description=docs.LIMIT_PARAM),
                offset: int = Query(default=0, ge=0, description=docs.OFFSET_PARAM)) -> EventListOut:
    return EventListOut(**_service(request).list(session_id, analysis_id, status, order, limit, offset))


@router.get("/{event_id}", response_model=EventOut, **docs.GET)
def get_event(request: Request, event_id: str = Path(description=docs.EVENT_ID_PARAM)) -> EventOut:
    return EventOut(**_service(request).get(event_id))


@router.patch("/{event_id}/confirmation", response_model=EventOut, **docs.CONFIRM)
def confirm_event(request: Request, body: ConfirmationIn,
                  event_id: str = Path(description=docs.EVENT_ID_PARAM)) -> EventOut:
    return EventOut(**_service(request).confirm(event_id, body.result))
