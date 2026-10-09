from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.codes import (GUARDIAN_DOC, RX_ID_DOC, RX_STATUS_DOC, SESSION_STATUS_DOC, STATE_DOC, MonitorState,
                               RxId, RxStatus, SessionStatus)


class PacketIn(BaseModel):
    ts: float | str | None = Field(default=None, description="수집 시각. UNIX 초 또는 ISO 8601. 없으면 서버 수신 시각",
                                   examples=[1791370800.125])
    rx: str = Field(description=f"수신기. 허용 값: {RX_ID_DOC} (대소문자·공백 무시). 그 외 값은 rejected_invalid로 버림",
                    examples=["RX1"], json_schema_extra={"enum": ["RX1", "RX2", "RX3"]})
    amplitude: list[float | None] = Field(description="서브캐리어 52개 진폭 (sub_0 ~ sub_51)",
                                          examples=[[12.5] * 52])


class PacketBatchIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "packets": [{"ts": 1791370800.125, "rx": "RX1", "amplitude": [12.5] * 52},
                    {"ts": 1791370800.155, "rx": "RX2", "amplitude": [9.8] * 52}],
    }})

    packets: list[PacketIn] = Field(description="CSI 패킷 묶음 (한 번에 최대 5000개)", max_length=5000)


class IngestOut(BaseModel):
    accepted: int = Field(description="엔진에 들어간 패킷 수")
    rejected_invalid: int = Field(description="시각·수신기 값이 잘못됐거나 서버 시각보다 1시간 넘게 미래라 버린 패킷 수")
    rejected_incomplete: int = Field(description="진폭이 52개가 아니거나 빈 값이 있어 버린 패킷 수")
    rejected_stale: int = Field(description="이미 받은 시각보다 이르거나 같아(중복·역순) 버린 패킷 수")
    frames: int = Field(description="이번 묶음으로 새로 계산된 0.5초 프레임 수")


class RxStatusOut(BaseModel):
    rx: RxId = Field(description=f"수신기. {RX_ID_DOC}")
    status: RxStatus = Field(description=f"연결 상태 코드. {RX_STATUS_DOC}")
    status_ko: str = Field(description="연결 상태 (화면 표시용 한국어)")
    packet_rate: float = Field(description="최근 2초 초당 패킷 수")
    last_packet_ts: float | None = Field(description="이 수신기의 마지막 패킷 시각")


class SessionOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "id": "4b8f3c9e-2d6a-4f0e-9a51-6f1d2c3b4a5e",
        "status": "running",
        "started_at": 1791370800.0,
        "stopped_at": None,
        "elapsed_sec": 4800.0,
        "last_packet_at": 1791375599.8,
        "seconds_since_last_packet": 2.0,
        "state": "low_motion",
        "state_ko": "움직임 적음",
        "motion_index": 0.0123,
        "activity_score": 0.0412,
        "rx": [{"rx": "RX1", "status": "ok", "status_ko": "정상", "packet_rate": 11.5,
                "last_packet_ts": 1791375599.8}],
        "event_count": 4,
        "unconfirmed_count": 1,
    }})

    id: str = Field(description="세션 ID")
    status: SessionStatus = Field(description=f"세션 상태. {SESSION_STATUS_DOC}")
    started_at: float = Field(description="관찰 시작 시각")
    stopped_at: float | None = Field(description="관찰 종료 시각")
    elapsed_sec: float = Field(description="관찰 시간(초)")
    last_packet_at: float | None = Field(description="마지막 신호 수신 시각 (수집기 시각)")
    seconds_since_last_packet: float | None = Field(description="마지막 신호 수신 후 지난 초 (\"2초 전\" 표시용)")
    state: MonitorState | None = Field(description=f"현재 상태 코드 (첫 판정 전에는 null). {STATE_DOC}")
    state_ko: str | None = Field(description="현재 상태 (화면 표시용 한국어)")
    motion_index: float | None = Field(description="현재 움직임 강도")
    activity_score: float | None = Field(description="현재 걷기 수준 활동 확률 0~1")
    rx: list[RxStatusOut] = Field(description="RX1~3 연결 상태")
    event_count: int = Field(description="감지된 주요 움직임 수")
    unconfirmed_count: int = Field(description="보호자 미확인 사건 수")


class SessionStartOut(SessionOut):
    created: bool = Field(description="새로 시작했으면 true, 이미 실행 중인 세션을 돌려줬으면 false")


class EventMarkerOut(BaseModel):
    id: int = Field(description="사건 번호")
    start_ts: float = Field(description="활동 시작 시각")
    alert_ts: float = Field(description="알림 시각")
    end_ts: float | None = Field(description="감지 종료 시각 (진행 중이면 null)")
    duration_sec: float = Field(description="지속 시간(초)")
    ongoing: bool = Field(description="아직 활동 중인지")
    guardian_result: str | None = Field(description=f"보호자 확인 결과. {GUARDIAN_DOC}, 미확인이면 null")


class SignalsOut(BaseModel):
    session_id: str
    from_ts: float = Field(description="조회 시작 시각")
    to_ts: float = Field(description="조회 끝 시각")
    step_sec: float = Field(description="점 사이 간격(초). 0.5초 프레임을 max_points에 맞게 묶은 결과")
    activity_threshold: float | None = Field(description="활동 판정 기준값 (그래프 기준선)")
    count: int = Field(description="점 개수")
    ts: list[float] = Field(description="x축 시각")
    motion_index: list[float | None] = Field(description="움직임 강도 (기준선 준비 중이면 null → 빈 구간)")
    activity_score: list[float | None] = Field(description="걷기 수준 활동 확률 (묶은 구간의 최댓값)")
    state: list[MonitorState] = Field(description=f"점마다 상태 코드. {STATE_DOC}")
    event_id: list[int | None] = Field(description="그 시점의 사건 번호 (주요 움직임 구간 강조용)")
    signal_ok: list[bool] = Field(description="수신 상태 정상 여부")
    heatmap: list[list[float] | None] | None = Field(description="CSI 히트맵 열 (점마다 서브캐리어 52개 평균 진폭)")
    events: list[EventMarkerOut] = Field(description="구간 안의 활동 사건 (그래프 표시용)")
