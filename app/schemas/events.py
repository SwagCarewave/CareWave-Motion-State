from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.codes import EVENT_LABEL_DOC, EVENT_STATUS_DOC, GUARDIAN_DOC, EventStatus, GuardianResult

_EXAMPLE = {
    "id": "0b6f8c1e-6a0f-4f7e-9d3a-2a51c4e7d9b0",
    "number": 1,
    "session_id": "a4ac705f-0fcd-4cad-9046-7573094b959e",
    "analysis_id": None,
    "source": "live",
    "title": "지속 활동 감지",
    "started_at": 1782371778.841,
    "alerted_at": 1782371781.841,
    "ended_at": 1782371795.341,
    "duration_sec": 16.5,
    "ongoing": False,
    "alert_message": "취침 모드 중 움직임이 약 3초간 이어지고 있습니다. 현장을 확인해 주세요",
    "status": "unconfirmed",
    "status_ko": "보호자 확인 대기",
    "guardian_result": None,
    "confirmed_at": None,
}


class EventOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE})

    id: str = Field(description="사건 ID")
    number: int = Field(description="세션(또는 분석) 안에서의 사건 번호. 그래프 프레임의 event_id와 같음")
    session_id: str | None = Field(description="실시간 관찰 세션 ID (CSV 분석 사건이면 null)")
    analysis_id: str | None = Field(description="CSV 분석 ID (실시간 사건이면 null)")
    source: str = Field(description="`live` = 실시간 관찰 / `csv` = CSV 분석")
    title: str = Field(description="목록 제목")
    started_at: float = Field(description="활동 시작 시각")
    alerted_at: float = Field(description="알림 시각")
    ended_at: float | None = Field(description="감지 종료 시각 (활동 중이면 null)")
    duration_sec: float = Field(description="지속 시간(초)")
    label: str = Field(description=f"그래프 표시 문구. {EVENT_LABEL_DOC}")
    ongoing: bool = Field(description="지금도 활동이 감지되는 중인지 (보호자 확인 여부와 무관)")
    alert_message: str | None = Field(description="알림 문구")
    status: EventStatus = Field(description=f"확인 상태. {EVENT_STATUS_DOC}")
    status_ko: str = Field(description="목록·상세에 표시할 확인 상태 문구 (예: 보호자 확인 대기, 정상 활동 확인 완료)")
    guardian_result: str | None = Field(description=f"보호자 확인 결과. {GUARDIAN_DOC}, 미확인이면 null")
    confirmed_at: float | None = Field(description="보호자 확인 시각")


class EventCountsOut(BaseModel):
    all: int = Field(description="전체 사건 수 (탭 '전체')")
    unconfirmed: int = Field(description="미확인 사건 수 (탭 '미확인', '미확인 N건')")
    confirmed: int = Field(description="확인 완료 사건 수 (탭 '확인 완료')")


class EventListOut(BaseModel):
    counts: EventCountsOut = Field(description="필터와 관계없이 전체·미확인·확인 완료 개수")
    total: int = Field(description="status 필터를 적용한 사건 수 (페이지 계산용)")
    items: list[EventOut] = Field(description="사건 목록")


class ConfirmationIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"result": "정상 활동"}})

    result: GuardianResult = Field(description=f"보호자가 현장에서 확인한 결과. {GUARDIAN_DOC}")
