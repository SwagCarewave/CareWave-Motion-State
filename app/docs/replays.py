from __future__ import annotations

from app.schemas.system import ErrorOut

_NOT_FOUND = {404: {"model": ErrorOut, "description": "재생 정보 없음 → 다시 만들기 (replay_not_found)"}}

CREATE = {
    "summary": "CSV 재생 시작",
    "description": "완료된 분석으로 재생을 만듭니다. 재생 시각은 서버가 계산하며, 그래프·히트맵 커서와 슬라이더에 씁니다. "
                   "WebSocket `/ws/replays/{id}`에 연결하면 재생 중 0.5초마다 재생 시각과 그 사이 프레임을 받습니다. "
                   "ESP 6대 녹화 분석이면 프레임은 스켈레톤이고, 접속 직후 `state` 메시지에 그릴 모델 정보(`model`)와 "
                   "현재 시각의 스켈레톤(`current`)이, 이후 `tick`마다 `current`가 옵니다. 재생바를 옮기면 그 시각의 `current`가 바로 옵니다. "
                   "서버가 다시 시작되면 재생 정보는 사라지므로(404) 다시 만들면 됩니다.",
    "responses": {404: {"model": ErrorOut, "description": "없는 분석 (analysis_not_found)"},
                  409: {"model": ErrorOut, "description": "분석이 아직 끝나지 않음 (analysis_not_ready)"}},
}

GET = {
    "summary": "CSV 재생 상태 조회",
    "description": "현재 재생 시각, 재생·일시정지 상태, 배속, 구간을 반환합니다.",
    "responses": _NOT_FOUND,
}

UPDATE = {
    "summary": "CSV 재생 조작",
    "description": "재생·일시정지, 재생 시각 이동(슬라이더), 배속(1·4·16), 재생 구간 변경을 한 번에 또는 따로 보냅니다. "
                   "보낸 항목만 바뀝니다.",
    "responses": _NOT_FOUND,
}

REPLAY_ID_PARAM = "재생 ID"
