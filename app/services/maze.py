from __future__ import annotations

from app.repositories import Repositories
from app.timeutil import from_iso

RANKING_ORDER = ("duration_sec", "created_at")
PAGE_SIZE = 1000


def _view(row: dict) -> dict:
    total = int(row["duration_sec"])
    return {
        "id": row["id"],
        "name": row["name"],
        "minutes": total // 60,
        "seconds": total % 60,
        "duration_sec": total,
        "created_at": from_iso(row["created_at"]),
    }


class MazeService:
    def __init__(self, repos: Repositories):
        self.table = repos.maze_records

    def create(self, name: str, minutes: int, seconds: int) -> dict:
        row = self.table.insert({"name": name, "duration_sec": minutes * 60 + seconds})
        return _view(row)

    def ranking(self, limit: int | None = None) -> dict:
        rows = self._ordered(None if limit is None else limit + 1)
        has_more = limit is not None and len(rows) > limit
        rows = rows[:limit] if limit is not None else rows
        return {"records": [{"rank": i, **_view(r)} for i, r in enumerate(rows, start=1)], "has_more": has_more}

    def _ordered(self, limit: int | None) -> list[dict]:
        rows, offset = [], 0
        while limit is None or len(rows) < limit:
            size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(rows))
            page = self.table.list(order_by=RANKING_ORDER, limit=size, offset=offset)
            rows.extend(page)
            if len(page) < size:
                break
            offset += size
        return rows
