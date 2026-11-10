from __future__ import annotations

from app.schemas.system import ErrorOut

_UNAVAILABLE = {503: {"model": ErrorOut, "description": "6대 자세 모델이 꺼져 있거나 불러오지 못함 (pose_unavailable)"}}

STATUS = {
    "summary": "6대 자세 추정 상태",
    "description": "ESP 6대(30개 선) 수신 상태와 사용 중인 모델, 가장 최근 추정 결과를 반환합니다. "
                   "선 상태는 최근 2초 동안 선마다 초당 받은 레코드 수(`rate_hz`)로 정합니다. 20Hz 이상이면 정상, "
                   "미만이면 신호 부족, 2초 넘게 안 들어오면 연결 끊김입니다. 머리·어깨 모델은 30개 선이 모두 0.15초 안에 들어와야 추정이 나오고, "
                   "13관절 모델은 빠진 선의 마지막 값을 이어 씁니다. `calibration`은 시작 보정 진행 상황입니다.",
    "responses": _UNAVAILABLE,
}

LATEST = {
    "summary": "가장 최근 자세 추정",
    "description": "추정(머리·어깨 모델 0.2초마다, 13관절 모델 0.05초마다) 중 가장 최근 1개를 반환합니다. 아직 추정이 없으면 `latest`가 null입니다. "
                   "화면을 계속 갱신할 때는 WebSocket `/ws/pose`를 쓰세요.",
    "responses": _UNAVAILABLE,
}

SIGNALS = {
    "summary": "6대 움직임 강도·신호 변화 그래프",
    "description": "ESP 4대 실시간 그래프(`GET /api/sessions/{id}/signals`)와 **같은 형식**의 6대 그래프입니다. 0.5초마다 한 점, 최근 1시간 보관. "
                   "`motion_index`(움직임 강도)는 30개 선의 CSI 스펙트럼 변화량을 시작 20초 기준선과 비교한 값이라 모델과 상관없고, "
                   "`activity_score`는 지금 모델의 역동성 점수(게임 점수)라 모델에 따라 다릅니다. `heatmap`은 서브캐리어 52개 평균 진폭(30개 선 평균), "
                   "`activity_threshold`는 0.8(게임 우 방향 기준선). 사건은 없으므로 `events`·`event_id`는 비어 있습니다. "
                   "새 점은 WebSocket `/ws/pose`의 `frames` 메시지로도 옵니다.",
    "responses": _UNAVAILABLE,
}

CALIBRATE = {
    "summary": "자세 모델 다시 보정",
    "description": "보정이 필요한 모델(`calibration.required`가 true, 예: 13관절 TCN)은 데이터가 들어오기 시작한 뒤 처음 "
                   "`calibration.seconds`초를 기준값으로 씁니다. `calibration.mode`가 stand(v3, 10초)면 **사람이 서서 가만히**, "
                   "empty_room(v5, 120초)이면 **사람이 ESP 6대 영역 밖으로 나가 빈 방**이어야 합니다. 빈 방 모델은 이 기준값으로 "
                   "사람 유무(`person_present`)도 판단합니다. 조건이 깨졌다면 이 API로 "
                   "보정을 처음부터 다시 시작합니다. 보정 중에는 추정이 나오지 않고 `calibration.progress`가 0→1로 올라갑니다. "
                   "보정이 필요 없는 모델은 입력 창만 비워집니다.",
    "responses": _UNAVAILABLE,
}

MODELS = {
    "summary": "자세 모델 목록",
    "description": "`models/pose` 폴더에 있는 모델 버전과 현재 사용 중인 모델(`active`)을 반환합니다.",
    "responses": _UNAVAILABLE,
}

SWITCH = {
    "summary": "사용할 자세 모델 바꾸기",
    "description": "서버를 다시 시작하지 않고 모델을 바꿉니다. 바꾸는 순간 입력 창이 비워져, 새 모델의 입력 길이(2초 또는 3.2초)와 "
                   "보정 시간(필요한 모델만 10초)이 지난 뒤부터 추정이 나옵니다. WebSocket으로 `model` 메시지가 가므로 화면은 "
                   "그 안의 `points`·`skeleton`으로 다시 그리면 됩니다. 서버를 다시 시작하면 환경변수 `CAREWAVE_POSE_MODEL`의 모델로 돌아갑니다.",
    "responses": {**_UNAVAILABLE, 404: {"model": ErrorOut, "description": "없는 모델 (pose_model_not_found)"},
                  409: {"model": ErrorOut, "description": "모델 파일 해시·구조 검사 실패 (pose_model_invalid)"}},
}
