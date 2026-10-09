from __future__ import annotations

from app.schemas.codes import EVENT_FILTER_DOC
from app.schemas.system import ErrorOut

_NOT_FOUND = {404: {"model": ErrorOut, "description": "없는 사건 (event_not_found)"}}

LIST = {
    "summary": "활동 사건 목록",
    "description": "감지된 활동 사건 목록과 전체·미확인·확인 완료 개수를 반환합니다. "
                   "실시간 대시보드는 session_id로 이번 관찰의 사건만, CSV 화면은 analysis_id로 그 분석의 사건만 조회합니다. "
                   "둘 다 비우면 모든 사건을 반환합니다.",
}

GET = {
    "summary": "활동 사건 상세",
    "description": "사건의 시작·알림·종료 시각, 지속 시간, 알림 문구, 보호자 확인 상태와 결과를 반환합니다.",
    "responses": _NOT_FOUND,
}

CONFIRM = {
    "summary": "보호자 확인 결과 저장",
    "description": "보호자가 현장에서 확인한 결과를 저장하고 상황을 종료합니다. 다시 보내면 결과와 확인 시각을 덮어씁니다. "
                   "실시간 관찰 중인 사건이면 대시보드의 '보호자 확인 대기' 상태가 풀리고 WebSocket으로 `event` 메시지가 갑니다.",
    "responses": _NOT_FOUND,
}

SESSION_PARAM = "실시간 관찰 세션 ID로 거르기"
ANALYSIS_PARAM = "CSV 분석 ID로 거르기"
STATUS_PARAM = f"확인 상태로 거르기. {EVENT_FILTER_DOC}"
ORDER_PARAM = "시작 시각 정렬. `asc` = 오래된 순 / `desc` = 최신 순"
LIMIT_PARAM = "최대 개수 (1~500)"
OFFSET_PARAM = "건너뛸 개수 (페이지)"
EVENT_ID_PARAM = "사건 ID"
