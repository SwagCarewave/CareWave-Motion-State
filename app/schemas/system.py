from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class HealthOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "status": "ok",
        "model": "lying_walking_v2",
        "model_verified": True,
        "storage": "supabase",
        "checks": {"database": "ok", "storage": "ok"},
        "collector": {"enabled": True, "listening": True, "port": 5005, "received": 120530, "rejected": 12,
                      "dropped_no_session": 0, "last_packet_at": 1791375599.8},
        "version": "0.1.0",
    }})

    status: str = Field(description="ok: 모두 정상 | degraded: DB 또는 Storage 연결 문제")
    model: str = Field(description="사용 중인 모델 이름")
    model_verified: bool = Field(description="모델 파일 해시 검사 통과 여부")
    storage: str = Field(description="저장소 종류 (memory | supabase)")
    collector: dict = Field(default_factory=dict,
                            description="ESP UDP 수신 상태: enabled, listening, port, received, rejected, "
                                        "dropped_no_session(취침 모드가 꺼져 버린 패킷), last_packet_at")
    checks: dict[str, str] = Field(description="DB·Storage 연결 확인 결과 (ok | unavailable: 연결 불가 | error: 오류 응답)")
    version: str = Field(description="API 버전")


class ErrorDetail(BaseModel):
    code: str = Field(description="오류 코드", examples=["missing_columns"])
    message: str = Field(description="화면에 보여줄 한국어 메시지",
                         examples=["필수 열이 없습니다. 타임스탬프, 수신기, CSI 진폭 항목을 확인하세요."])
    detail: dict = Field(default_factory=dict, description="추가 정보", examples=[{"missing": ["rx"]}])


class ErrorOut(BaseModel):
    error: ErrorDetail


class CodesOut(BaseModel):
    session_status: list[dict] = Field(description="세션 상태 code·label")
    rx: list[str] = Field(description="수신기 이름")
    rx_status: list[dict] = Field(description="RX 연결 상태 code·label·rule")
    state: list[dict] = Field(description="관찰 상태 code·label·rule")
    guardian_result: list[str] = Field(description="보호자 확인 결과로 보낼 수 있는 값")
    event_status: list[dict] = Field(description="사건 확인 상태 code·label")
    event_filter: list[dict] = Field(description="사건 목록 탭 code·label")
    event_label: list[dict] = Field(description="그래프 사건 표시 문구 label·rule")
    analysis_status: list[dict] = Field(description="CSV 분석 상태 code·label")
    analysis_error: list[dict] = Field(description="CSV 분석 실패 코드 code·label")
    replay_status: list[dict] = Field(description="재생 상태 code·label")
    replay_speed: list[dict] = Field(description="재생 배속 value·label")
    replay_message: list[dict] = Field(description="재생 WebSocket 메시지 type·rule")
    signal_window: list[dict] = Field(description="그래프 기간 선택지 seconds·label")
    stream_message: list[dict] = Field(description="WebSocket 메시지 type·rule")
    error: list[dict] = Field(description="오류 code·http·meaning")
