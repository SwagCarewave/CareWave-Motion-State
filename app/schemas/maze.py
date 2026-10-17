from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NAME_MAX = 20
MINUTES_MAX = 999

_RECORD_EXAMPLE = {
    "id": "3c9a7e21-5b4d-4f8e-a1c2-6d0b9e8f7a35",
    "name": "홍길동",
    "minutes": 1,
    "seconds": 23,
    "duration_sec": 83,
    "created_at": 1791420000.5,
}


class MazeRecordIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"name": "홍길동", "minutes": 1, "seconds": 23}})

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)] = Field(
        description=f"플레이어 이름 (앞뒤 공백 제거 후 1~{NAME_MAX}자, 같은 이름 여러 번 저장 가능)")
    minutes: int = Field(ge=0, le=MINUTES_MAX, description=f"걸린 시간의 분 (0~{MINUTES_MAX})")
    seconds: int = Field(ge=0, le=59, description="걸린 시간의 초 (0~59)")


class MazeRecordOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": _RECORD_EXAMPLE})

    id: str = Field(description="기록 ID")
    name: str = Field(description="플레이어 이름")
    minutes: int = Field(description="걸린 시간의 분")
    seconds: int = Field(description="걸린 시간의 초 (0~59)")
    duration_sec: int = Field(description="걸린 시간 전체(초). minutes × 60 + seconds")
    created_at: float = Field(description="저장 시각")


class MazeRankOut(MazeRecordOut):
    model_config = ConfigDict(json_schema_extra={"example": {"rank": 1, **_RECORD_EXAMPLE}})

    rank: int = Field(description="순위 (1부터). 시간이 같으면 먼저 저장한 기록이 앞 순위")


class MazeRankingOut(BaseModel):
    records: list[MazeRankOut] = Field(description="걸린 시간이 짧은 순 기록. 시간이 같으면 먼저 저장한 순")
    has_more: bool = Field(description="limit 뒤에 기록이 더 있는지 ('전체 보기' 버튼 표시용). limit이 없으면 항상 false")
