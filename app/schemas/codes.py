from __future__ import annotations

from typing import Literal

from app.services.engine import FINAL_GUARDIAN_RESULTS, RX_STATUS_KO
from motion_state.csi_io import RX_IDS
from motion_state.monitor_v2 import STATE_KO

SessionStatus = Literal["running", "stopped"]
RxId = Literal["RX1", "RX2", "RX3"]
RxStatus = Literal["ok", "weak", "lost", "none"]
MonitorState = Literal["baseline_prep", "low_motion", "observing", "sustained_activity", "signal_check",
                       "awaiting_confirmation"]
GuardianResult = Literal["정상 활동", "도움 필요", "잘못된 감지"]
StreamMessage = Literal["snapshot", "frames", "status", "alert", "event", "stopped"]
EventStatus = Literal["unconfirmed", "confirmed"]
EventFilter = Literal["all", "unconfirmed", "confirmed"]

EVENT_STATUS_KO = {"unconfirmed": "보호자 확인 대기", "confirmed": "확인 완료"}
EVENT_FILTER_KO = {"all": "전체", "unconfirmed": "미확인", "confirmed": "확인 완료"}

SESSION_STATUS_KO = {"running": "관찰 중", "stopped": "종료"}

RX_STATUS_RULE = {
    "ok": "최근 2초 동안 초당 패킷 3개 이상",
    "weak": "최근 2초 동안 초당 패킷 3개 미만",
    "lost": "마지막 패킷 이후 5초 이상 수신 없음",
    "none": "이 세션에서 받은 패킷이 없음",
}

STATE_RULE = {
    "baseline_prep": "시작 직후 기준선을 잡는 중 (약 10~20초). 움직임 강도는 null",
    "low_motion": "큰 움직임 없음",
    "observing": "걷기 수준 움직임이 시작됐지만 아직 3초가 안 됨",
    "sustained_activity": "걷기 수준 움직임이 3초 이상 이어짐 → 활동 사건 생성·알림",
    "signal_check": "수신기 패킷이 부족해 판단할 수 없음",
    "awaiting_confirmation": "움직임은 멈췄지만 보호자가 확인하지 않은 사건이 남아 있음",
}

STREAM_MESSAGE_RULE = {
    "snapshot": "접속 직후 1번. 현재 세션 상태(session)와 최근 프레임(frames, 최대 120개)",
    "frames": "새 0.5초 프레임 묶음(frames). 그래프·히트맵에 이어 붙임",
    "status": "1초마다. RX 상태(rx), 마지막 수신 후 초(seconds_since_last_packet)",
    "alert": "지속 활동 알림이 새로 생겼을 때. ts, event_id(사건 번호), event_uuid(사건 ID), message",
    "event": "사건이 저장·갱신·보호자 확인됐을 때. id(사건 ID), number(사건 번호) → 목록·상세 다시 조회",
    "stopped": "세션이 종료됨. 이후 서버가 연결을 닫음",
}

ERROR_CODES = {
    "invalid_request": (422, "요청 본문·쿼리 형식 오류"),
    "invalid_format": (422, "CSV를 읽을 수 없음"),
    "missing_columns": (422, "CSV 필수 열 없음 (timestamp, rx, sub_0~sub_51)"),
    "no_valid_rows": (422, "CSV에 유효한 행이 없음"),
    "session_not_found": (404, "관찰 세션 없음"),
    "session_stopped": (409, "이미 종료된 세션"),
    "event_not_found": (404, "활동 사건 없음"),
    "database_unavailable": (503, "DB 연결 실패"),
    "storage_unavailable": (503, "Storage 연결 실패"),
    "repository_error": (502, "저장소가 요청을 거부함"),
}

SIGNAL_WINDOWS = {600: "최근 10분", 1800: "최근 30분", 3600: "최근 1시간"}


def describe(labels: dict, rules: dict | None = None) -> str:
    lines = []
    for code, label in labels.items():
        rule = (rules or {}).get(code)
        lines.append(f"`{code}` = {label}" + (f" ({rule})" if rule else ""))
    return " / ".join(lines)


SESSION_STATUS_DOC = describe(SESSION_STATUS_KO)
RX_ID_DOC = " / ".join(f"`{rx}`" for rx in RX_IDS)
RX_STATUS_DOC = describe(RX_STATUS_KO, RX_STATUS_RULE)
STATE_DOC = describe(STATE_KO, STATE_RULE)
GUARDIAN_DOC = " / ".join(f"`{r}`" for r in FINAL_GUARDIAN_RESULTS)
WINDOW_DOC = describe({str(k): v for k, v in SIGNAL_WINDOWS.items()})
EVENT_STATUS_DOC = describe(EVENT_STATUS_KO)
EVENT_FILTER_DOC = describe(EVENT_FILTER_KO)


def catalog() -> dict:
    return {
        "session_status": [{"code": k, "label": v} for k, v in SESSION_STATUS_KO.items()],
        "rx": list(RX_IDS),
        "rx_status": [{"code": k, "label": v, "rule": RX_STATUS_RULE[k]} for k, v in RX_STATUS_KO.items()],
        "state": [{"code": k, "label": v, "rule": STATE_RULE[k]} for k, v in STATE_KO.items()],
        "guardian_result": list(FINAL_GUARDIAN_RESULTS),
        "event_status": [{"code": k, "label": v} for k, v in EVENT_STATUS_KO.items()],
        "event_filter": [{"code": k, "label": v} for k, v in EVENT_FILTER_KO.items()],
        "signal_window": [{"seconds": k, "label": v} for k, v in SIGNAL_WINDOWS.items()],
        "stream_message": [{"type": k, "rule": v} for k, v in STREAM_MESSAGE_RULE.items()],
        "error": [{"code": k, "http": s, "meaning": m} for k, (s, m) in ERROR_CODES.items()],
    }
