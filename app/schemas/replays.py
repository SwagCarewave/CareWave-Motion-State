from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.codes import REPLAY_SPEED_DOC, REPLAY_STATUS_DOC, ReplayAction, ReplaySpeed, ReplayStatus


class ReplayCreateIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "analysis_id": "9a1d3c5e-7b2f-4e8a-b6c4-2d1e0f9a8b7c", "speed": 1, "play": True}})

    analysis_id: str = Field(description="재생할 분석 ID (완료된 분석)")
    from_ts: float | None = Field(default=None, description="재생 구간 시작 (비우면 기록 시작)")
    to_ts: float | None = Field(default=None, description="재생 구간 끝 (비우면 기록 끝)")
    speed: ReplaySpeed = Field(default=1, description=f"배속. {REPLAY_SPEED_DOC}")
    position_ts: float | None = Field(default=None, description="처음 재생 시각 (비우면 구간 시작)")
    play: bool = Field(default=False, description="true면 바로 재생, false면 일시정지 상태로 시작")


class ReplayUpdateIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"action": "pause"}})

    action: ReplayAction | None = Field(default=None, description="`play` = 재생 / `pause` = 일시정지. 끝난 뒤 play면 처음부터")
    position_ts: float | None = Field(default=None, description="재생 시각 이동 (슬라이더). 구간 밖이면 구간 끝으로 맞춤")
    speed: ReplaySpeed | None = Field(default=None, description=f"배속 변경. {REPLAY_SPEED_DOC}")
    from_ts: float | None = Field(default=None, description="재생 구간 시작 변경")
    to_ts: float | None = Field(default=None, description="재생 구간 끝 변경")


class ReplayOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "id": "c2d4e6f8-1a3b-4c5d-9e7f-0a1b2c3d4e5f",
        "analysis_id": "9a1d3c5e-7b2f-4e8a-b6c4-2d1e0f9a8b7c",
        "file_id": "5f0c2b9e-3a71-4d6c-8e2f-1b9a7c4d6e10",
        "status": "playing",
        "position_ts": 1791382140.0,
        "speed": 1,
        "from_ts": 1791378000.0,
        "to_ts": 1791414000.0,
        "record_start": 1791378000.0,
        "record_end": 1791414000.0,
        "progress": 0.115,
    }})

    id: str = Field(description="재생 ID")
    analysis_id: str = Field(description="분석 ID")
    file_id: str | None = Field(description="파일 ID")
    status: ReplayStatus = Field(description=f"재생 상태. {REPLAY_STATUS_DOC}")
    position_ts: float = Field(description="현재 재생 시각 ('재생 시각 10월 05일 23:09', 그래프·히트맵 커서)")
    speed: ReplaySpeed = Field(description=f"배속. {REPLAY_SPEED_DOC}")
    from_ts: float = Field(description="재생 구간 시작 (슬라이더 왼쪽 끝)")
    to_ts: float = Field(description="재생 구간 끝 (슬라이더 오른쪽 끝)")
    record_start: float = Field(description="기록 시작")
    record_end: float = Field(description="기록 끝")
    progress: float = Field(description="구간 안 재생 진행률 0~1 (슬라이더 위치)")
