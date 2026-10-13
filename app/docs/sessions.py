from __future__ import annotations

from app.schemas.codes import SESSION_STATUS_DOC, WINDOW_DOC
from app.schemas.sessions import SessionStartOut
from app.schemas.system import ErrorOut

_NOT_FOUND = {404: {"model": ErrorOut, "description": "없는 세션 (session_not_found)"}}

START = {
    "summary": "취침 모드 관찰 시작",
    "description": "관찰 세션을 시작합니다. 이미 실행 중인 세션이 있으면 새로 만들지 않고 그 세션을 200으로 돌려줍니다.",
    "responses": {200: {"model": SessionStartOut, "description": "이미 실행 중인 세션"}},
}

LIST = {
    "summary": "관찰 세션 목록",
    "description": "최근 세션 목록입니다. 대시보드 첫 화면에서는 status=running 으로 진행 중인 세션을 찾습니다.",
}

GET = {
    "summary": "관찰 상태 조회",
    "description": "관찰 시작 시각, 관찰 시간, RX1~3 연결 상태, 마지막 신호 수신, 현재 상태와 사건 수를 반환합니다. "
                   "종료된 세션은 종료 시점의 RX 상태와 저장된 사건 수를 돌려줍니다.",
    "responses": _NOT_FOUND,
}

STOP = {
    "summary": "취침 모드 관찰 종료",
    "description": "세션을 종료합니다. 남은 프레임, 활동 사건, 마지막 RX 상태를 저장합니다. 이미 종료된 세션이면 그대로 돌려줍니다.",
    "responses": _NOT_FOUND,
}

PACKETS = {
    "summary": "CSI 패킷 수신",
    "description": "수집기(ESP)가 받은 CSI 패킷을 묶어서 보냅니다. 패킷마다 엔진에 넣어 0.5초 프레임을 계산합니다. "
                   "잘못된 패킷은 묶음 전체를 거부하지 않고 개수만 셉니다. "
                   "같은 수신기에서 이미 받은 시각보다 이른 패킷과, 같은 시각에 진폭까지 같은 패킷은 중복으로 보고 버리므로 "
                   "재전송해도 안전합니다(서버 재시작 후에도 유지). 같은 시각이라도 진폭이 다르면 다른 패킷으로 받습니다. "
                   "서버 시각보다 1시간 넘게 미래인 패킷은 버립니다. 이전 패킷보다 10분 넘게 건너뛴 패킷이 오면 "
                   "판정을 새로 시작하므로 약 10~20초 동안 기준선 준비 상태가 됩니다.",
    "responses": {**_NOT_FOUND, 409: {"model": ErrorOut, "description": "종료된 세션 (session_stopped)"}},
}

SIGNALS = {
    "summary": "그래프·히트맵 구간 조회",
    "description": "움직임 강도 그래프와 CSI 히트맵 데이터를 열 형식으로 반환합니다(같은 인덱스끼리 한 점). "
                   "window 또는 from/to로 구간을 정합니다. 점이 max_points보다 많으면 일정 간격으로 묶습니다.",
    "responses": _NOT_FOUND,
}

RECORDING = {
    "summary": "실시간 녹화 상태",
    "description": "취침 모드를 시작하면 받은 CSI 원본(raw)을 서버 디스크에 CSV로 녹화합니다. "
                   "새 세션을 시작하면 이전 녹화는 바로 지웁니다. 종료 후 보관 시간(기본 5분)이 지나면 지웁니다. "
                   "첫 패킷부터 최대 길이(기본 15분)를 넘으면 녹화를 버립니다. downloadable이 true일 때 CSV 버튼을 켭니다.",
    "responses": _NOT_FOUND,
}

RECORDING_CSV = {
    "summary": "실시간 녹화 CSV 받기",
    "description": "녹화한 CSI 원본을 CSV 파일로 내려받습니다. 형식은 수집기(collect_csi.py)와 같아서 "
                   "POST /api/files 로 그대로 올리면 분석·그래프가 됩니다. 녹화 상태가 ready일 때만 받을 수 있습니다.",
    "responses": {
        200: {"content": {"text/csv": {}}, "description": "CSV 파일 (Content-Disposition: attachment)"},
        **_NOT_FOUND,
        409: {"model": ErrorOut, "description": "받을 수 있는 녹화 없음 (recording_unavailable, detail.status)"},
    },
}

STATUS_PARAM = f"세션 상태로 거르기. {SESSION_STATUS_DOC}. 비우면 전체"
LIMIT_PARAM = "최대 개수 (1~100)"
WINDOW_PARAM = f"최근 N초 (10~3600). 화면 선택지: {WINDOW_DOC}. from을 주면 무시"
FROM_PARAM = "시작 시각 (UNIX 초)"
TO_PARAM = "끝 시각 (UNIX 초). 비우면 마지막 프레임"
MAX_POINTS_PARAM = "최대 점 개수 (10~7200)"
HEATMAP_PARAM = "히트맵 포함 여부. false면 응답이 가벼워짐"
