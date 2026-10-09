from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.codes import ANALYSIS_ERROR_DOC, ANALYSIS_STATUS_DOC, AnalysisStatus

_FILE_EXAMPLE = {
    "id": "5f0c2b9e-3a71-4d6c-8e2f-1b9a7c4d6e10",
    "filename": "night_csi_20261006.csv",
    "size_bytes": 2176086,
    "rows": 4024,
    "packets": 3710,
    "incomplete_rows": 314,
    "receivers": ["RX1", "RX2", "RX3"],
    "packets_by_rx": {"RX1": 1240, "RX2": 1236, "RX3": 1234},
    "record_start": 1791378000.0,
    "record_end": 1791414000.0,
    "duration_sec": 36000.0,
    "uploaded_at": 1791420000.5,
    "latest_analysis": {"id": "9a1d3c5e-7b2f-4e8a-b6c4-2d1e0f9a8b7c", "status": "succeeded", "progress": 1.0,
                        "event_count": 3, "error_code": None, "error_message": None, "finished_at": 1791420090.2},
}


class LatestAnalysisOut(BaseModel):
    id: str = Field(description="분석 ID")
    status: AnalysisStatus = Field(description=f"분석 상태. {ANALYSIS_STATUS_DOC}")
    progress: float = Field(description="진행률 0~1")
    event_count: int | None = Field(description="감지된 활동 사건 수 (완료 전에는 null)")
    error_code: str | None = Field(description=f"실패 코드. {ANALYSIS_ERROR_DOC}")
    error_message: str | None = Field(description="실패 안내 문구 (화면 표시용)")
    finished_at: float | None = Field(description="분석이 끝난 시각")


class FileOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": _FILE_EXAMPLE})

    id: str = Field(description="파일 ID")
    filename: str = Field(description="올린 파일 이름 (등록된 파일 목록 표시용)")
    size_bytes: int = Field(description="파일 크기(바이트)")
    rows: int = Field(description="CSV 행 수")
    packets: int = Field(description="분석에 쓰는 유효 패킷 수")
    incomplete_rows: int = Field(description="진폭 값이 빠져 제외한 행 수")
    receivers: list[str] = Field(description="파일에 들어 있는 수신기 (업로드 파일 확인의 '수신기 RX1·RX2·RX3')")
    packets_by_rx: dict[str, int] = Field(description="수신기별 패킷 수")
    record_start: float = Field(description="기록 시작 시각 (업로드 파일 확인·'기록 시간')")
    record_end: float = Field(description="기록 끝 시각")
    duration_sec: float = Field(description="기록 길이(초) ('기록 시간 10시간')")
    uploaded_at: float | None = Field(description="업로드 시각")
    latest_analysis: LatestAnalysisOut | None = Field(description="가장 최근 분석 (분석 전이면 null)")
