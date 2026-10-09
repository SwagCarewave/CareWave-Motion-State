from __future__ import annotations

from app.schemas.analyses import AnalysisStartOut
from app.schemas.system import ErrorOut

_NOT_FOUND = {404: {"model": ErrorOut, "description": "없는 분석 (analysis_not_found)"}}

START = {
    "summary": "CSV 분석 시작",
    "description": "등록된 파일을 분석합니다. 백그라운드에서 진행되므로 바로 응답하고, 진행률은 분석 조회로 확인합니다. "
                   "이 파일의 분석이 이미 진행 중이면 새로 만들지 않고 그 분석을 200으로 돌려줍니다. "
                   "실패한 뒤 '다시 시도'도 이 API입니다. 다시 분석하면 이전 분석의 보호자 확인 결과를 사건 번호로 옮겨 오고 이전 분석은 정리합니다.",
    "responses": {200: {"model": AnalysisStartOut, "description": "이미 진행 중인 분석"},
                  404: {"model": ErrorOut, "description": "없는 파일 (file_not_found)"}},
}

GET = {
    "summary": "CSV 분석 상태 조회",
    "description": "분석 상태와 진행률, 실패 코드·안내 문구, 완료 후 기록 시간과 사건 수를 반환합니다. "
                   "'CSV 분석 중' 화면은 이 API를 0.5~1초마다 불러 진행 막대를 갱신하고, succeeded가 되면 CSV 기록 조회 화면으로 이동합니다.",
    "responses": _NOT_FOUND,
}

SIGNALS = {
    "summary": "CSV 분석 그래프·히트맵 조회",
    "description": "분석 결과의 움직임 강도 그래프와 CSI 히트맵 데이터를 실시간 그래프와 같은 열 형식으로 반환합니다. "
                   "구간을 주지 않으면 기록 전체입니다. 점이 max_points보다 많으면 일정 간격으로 묶습니다.",
    "responses": {**_NOT_FOUND, 409: {"model": ErrorOut, "description": "분석이 아직 끝나지 않음 (analysis_not_ready)"},
                  410: {"model": ErrorOut, "description": "결과 파일 없음 → 다시 분석 (result_missing)"}},
}

ANALYSIS_ID_PARAM = "분석 ID"
WINDOW_PARAM = "기록 끝에서부터 최근 N초. 비우면 기록 전체"
FROM_PARAM = "시작 시각 (UNIX 초)"
TO_PARAM = "끝 시각 (UNIX 초). 비우면 기록 끝"
MAX_POINTS_PARAM = "최대 점 개수 (10~7200)"
HEATMAP_PARAM = "히트맵 포함 여부"
