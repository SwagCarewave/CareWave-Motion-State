from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.codes import ANALYSIS_ERROR_DOC, ANALYSIS_STATUS_DOC, AnalysisStatus
from app.schemas.sessions import EventMarkerOut, SignalsOut


class AnalysisOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "id": "9a1d3c5e-7b2f-4e8a-b6c4-2d1e0f9a8b7c",
        "file_id": "5f0c2b9e-3a71-4d6c-8e2f-1b9a7c4d6e10",
        "status": "running",
        "progress": 0.42,
        "attempt": 1,
        "frame_count": None,
        "event_count": None,
        "record_start": None,
        "record_end": None,
        "duration_sec": None,
        "error_code": None,
        "error_message": None,
        "started_at": 1791420010.1,
        "finished_at": None,
        "created_at": 1791420010.0,
    }})

    id: str = Field(description="분석 ID")
    file_id: str = Field(description="분석한 파일 ID")
    status: AnalysisStatus = Field(description=f"분석 상태. {ANALYSIS_STATUS_DOC}")
    progress: float = Field(description="진행률 0~1 (진행 막대)")
    attempt: int = Field(description="이 파일의 몇 번째 분석인지")
    frame_count: int | None = Field(description="계산된 0.5초 프레임 수 (완료 후)")
    event_count: int | None = Field(description="감지된 활동 사건 수 (완료 후, '감지된 활동 사건 N건')")
    record_start: float | None = Field(description="기록 시작 시각 (완료 후)")
    record_end: float | None = Field(description="기록 끝 시각 (완료 후)")
    duration_sec: float | None = Field(description="기록 길이(초) (완료 후)")
    error_code: str | None = Field(description=f"실패 코드. {ANALYSIS_ERROR_DOC}")
    error_message: str | None = Field(description="실패 안내 문구 ('분석하지 못했습니다' 화면 표시용)")
    started_at: float | None = Field(description="분석 시작 시각")
    finished_at: float | None = Field(description="분석이 끝난(성공·실패) 시각")
    created_at: float | None = Field(description="분석 요청 시각")


class AnalysisStartOut(AnalysisOut):
    created: bool = Field(description="새로 시작했으면 true, 이미 진행 중인 분석을 돌려줬으면 false")


class AnalysisSignalsOut(SignalsOut):
    session_id: str | None = Field(default=None, description="항상 null (CSV 분석)")
    analysis_id: str = Field(description="분석 ID")
    file_id: str | None = Field(description="파일 ID")
    events: list[EventMarkerOut] = Field(description="구간 안의 활동 사건 (그래프 표시·클릭 시 상세)")
