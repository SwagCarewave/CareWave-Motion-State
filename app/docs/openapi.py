from __future__ import annotations

from app.schemas.system import ErrorOut

TITLE = "CareWave Motion State API"

TAG_SYSTEM = "시스템"
TAG_LIVE = "실시간 관찰"

TAGS = [
    {"name": TAG_SYSTEM},
    {"name": TAG_LIVE},
]

COMMON_RESPONSES = {
    422: {"model": ErrorOut, "description": "요청 또는 파일 형식 오류"},
    503: {"model": ErrorOut, "description": "DB 또는 Storage 연결 실패"},
}

SWAGGER_UI = {"defaultModelsExpandDepth": 0, "displayRequestDuration": True}
