from __future__ import annotations

from fastapi import APIRouter, Path, Query, Request

from app.docs import analyses as docs
from app.docs.openapi import TAG_CSV
from app.schemas.analyses import AnalysisOut, AnalysisSignalsOut
from app.services.analysis import AnalysisService

router = APIRouter(prefix="/api/analyses", tags=[TAG_CSV])


def _analyses(request: Request) -> AnalysisService:
    return request.app.state.analyses


@router.get("/{analysis_id}", response_model=AnalysisOut, **docs.GET)
def get_analysis(request: Request, analysis_id: str = Path(description=docs.ANALYSIS_ID_PARAM)) -> AnalysisOut:
    return AnalysisOut(**_analyses(request).get(analysis_id))


@router.get("/{analysis_id}/signals", response_model=AnalysisSignalsOut, **docs.SIGNALS)
def get_analysis_signals(request: Request, analysis_id: str = Path(description=docs.ANALYSIS_ID_PARAM),
                         window: int | None = Query(default=None, ge=10, description=docs.WINDOW_PARAM),
                         from_ts: float | None = Query(default=None, alias="from", description=docs.FROM_PARAM),
                         to_ts: float | None = Query(default=None, alias="to", description=docs.TO_PARAM),
                         max_points: int = Query(default=1200, ge=10, le=7200, description=docs.MAX_POINTS_PARAM),
                         heatmap: bool = Query(default=True, description=docs.HEATMAP_PARAM)) -> AnalysisSignalsOut:
    data = _analyses(request).signals(analysis_id, window_sec=window, from_ts=from_ts, to_ts=to_ts,
                                      max_points=max_points, heatmap=heatmap)
    return AnalysisSignalsOut(**data)
