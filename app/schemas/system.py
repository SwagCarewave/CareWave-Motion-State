from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "name": "lying_walking_v2",
        "sha256": "89c3b1fab51e50f9817bfe5c9d07e213cd86fd97b3d95ab1ef330512a7bc9a0e",
        "model_type": "LogisticRegression",
        "feature_count": 60,
        "threshold": 0.5002,
        "smooth": 1,
        "lookahead_sec": 1.0,
    }})

    name: str = Field(description="모델 파일 이름")
    sha256: str = Field(description="모델 파일 SHA-256 해시")
    model_type: str = Field(description="분류기 종류")
    feature_count: int = Field(description="입력 특징 개수")
    threshold: float = Field(description="걷기 수준 활동 판정 기준값")
    smooth: int = Field(description="점수 평균에 쓰는 판정 개수")
    lookahead_sec: float = Field(description="판정 시점 이후로 보는 시간(초). 결과가 이만큼 늦게 나옴")


class HealthOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "status": "ok",
        "model": "lying_walking_v2",
        "model_verified": True,
        "storage": "supabase",
        "checks": {"database": "ok", "storage": "ok"},
        "version": "0.1.0",
    }})

    status: str = Field(description="ok: 모두 정상 | degraded: DB 또는 Storage 연결 문제")
    model: str = Field(description="사용 중인 모델 이름")
    model_verified: bool = Field(description="모델 파일 해시 검사 통과 여부")
    storage: str = Field(description="저장소 종류 (memory | supabase)")
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
    signal_window: list[dict] = Field(description="그래프 기간 선택지 seconds·label")
    stream_message: list[dict] = Field(description="WebSocket 메시지 type·rule")
    error: list[dict] = Field(description="오류 code·http·meaning")
