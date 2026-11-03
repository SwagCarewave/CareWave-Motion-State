from __future__ import annotations

from app.schemas.system import ErrorOut

TITLE = "CareWave Motion State API"

TAG_SYSTEM = "시스템"
TAG_LIVE = "실시간 관찰"
TAG_EVENTS = "활동 사건"
TAG_CSV = "CSV 기록"
TAG_MAZE = "미로 게임"
TAG_POSE = "6대 자세 추정"

TAGS = [
    {"name": TAG_SYSTEM},
    {"name": TAG_LIVE},
    {"name": TAG_EVENTS},
    {"name": TAG_CSV},
    {"name": TAG_MAZE},
    {"name": TAG_POSE, "description": "ESP 6대(30개 선) 스켈레톤·행동 추정. 실시간 화면은 WebSocket `/ws/pose`: 연결 직후 snapshot"
                                      "(model.points·skeleton으로 그림), 이후 pose(모델에 따라 0.05~0.2초마다), links(1초마다), "
                                      "calibration(보정 끝났을 때), model(모델 교체 시)."},
]

COMMON_RESPONSES = {
    422: {"model": ErrorOut, "description": "요청 또는 파일 형식 오류"},
    500: {"model": ErrorOut, "description": "서버 내부 오류"},
    503: {"model": ErrorOut, "description": "DB 또는 Storage 연결 실패"},
}

SWAGGER_UI = {"defaultModelsExpandDepth": 0, "displayRequestDuration": True}
