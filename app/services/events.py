from __future__ import annotations

import time
from typing import Any, Callable

from app.repositories import Repositories, RepositoryError
from app.services.engine import FINAL_GUARDIAN_RESULTS
from app.services.monitoring import SessionManager
from app.timeutil import from_iso, to_iso

EVENT_TITLE = "지속 활동 감지"
UNCONFIRMED_KO = "보호자 확인 대기"
PAGE_SIZE = 1000
ID_CHUNK = 100


class EventNotFound(LookupError):
    pass


def _fetch_all(table, filters: dict | None, order_by: str) -> list[dict]:
    rows, offset = [], 0
    while True:
        page = table.list(filters, order_by=order_by, limit=PAGE_SIZE, offset=offset)
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


class EventService:
    def __init__(self, repos: Repositories, sessions: SessionManager, clock: Callable[[], float] = time.time):
        self.repos = repos
        self.sessions = sessions
        self.clock = clock

    def list(self, session_id: str | None = None, analysis_id: str | None = None, status: str = "all",
             order: str = "asc", limit: int = 50, offset: int = 0) -> dict:
        filters: dict[str, Any] = {}
        if session_id:
            filters["session_id"] = session_id
        if analysis_id:
            filters["analysis_id"] = analysis_id
        rows = _fetch_all(self.repos.activity_events, filters or None, "started_at")
        views = self._views(rows)
        counts = {
            "all": len(views),
            "unconfirmed": sum(1 for v in views if v["status"] == "unconfirmed"),
            "confirmed": sum(1 for v in views if v["status"] == "confirmed"),
        }
        if status != "all":
            views = [v for v in views if v["status"] == status]
        views.sort(key=lambda v: (v["started_at"], v["number"]), reverse=order == "desc")
        return {"counts": counts, "total": len(views), "items": views[offset:offset + limit]}

    def get(self, event_id: str) -> dict:
        return self._views([self._row(event_id)])[0]

    def confirm(self, event_id: str, result: str) -> dict:
        if result not in FINAL_GUARDIAN_RESULTS:
            raise ValueError(f"result must be one of {FINAL_GUARDIAN_RESULTS}")
        row = self._row(event_id)
        self.repos.guardian_confirmations.upsert(
            {"event_id": row["id"], "result": result, "confirmed_at": to_iso(self.clock())}, ("event_id",))
        if row.get("session_id"):
            self.sessions.close_live_event(row["session_id"], row["event_no"], result)
        return self.get(event_id)

    def _row(self, event_id: str) -> dict:
        try:
            row = self.repos.activity_events.get(event_id)
        except RepositoryError as exc:
            if exc.status in (400, 404):
                raise EventNotFound(event_id) from exc
            raise
        if row is None:
            row = self.sessions.find_live_event(event_id)
        if row is None:
            raise EventNotFound(event_id)
        return row

    def _confirmations(self, ids: list[str]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for i in range(0, len(ids), ID_CHUNK):
            for c in self.repos.guardian_confirmations.list({"event_id__in": ids[i:i + ID_CHUNK]}):
                found[c["event_id"]] = c
        return found

    def _views(self, rows: list[dict]) -> list[dict]:
        confirmations = self._confirmations([r["id"] for r in rows])
        live_cache: dict[str, dict] = {}
        out = []
        for r in rows:
            live = {}
            if r.get("session_id"):
                if r["session_id"] not in live_cache:
                    live_cache[r["session_id"]] = self.sessions.live_event_state(r["session_id"])
                live = live_cache[r["session_id"]].get(r["event_no"], {})
            out.append(self._view(r, confirmations.get(r["id"]), live))
        return out

    @staticmethod
    def _view(row: dict, confirmation: dict | None, live: dict) -> dict:
        result = confirmation["result"] if confirmation else None
        ongoing = bool(live.get("ongoing"))
        ended = live.get("end_ts") if live else from_iso(row.get("ended_at"))
        return {
            "id": row["id"],
            "number": row["event_no"],
            "session_id": row.get("session_id"),
            "analysis_id": row.get("analysis_id"),
            "source": "live" if row.get("session_id") else "csv",
            "title": EVENT_TITLE,
            "started_at": live.get("start_ts") or from_iso(row["started_at"]),
            "alerted_at": live.get("alert_ts") or from_iso(row["alerted_at"]),
            "ended_at": ended,
            "duration_sec": live.get("duration_sec", row.get("duration_sec") or 0.0),
            "ongoing": ongoing,
            "alert_message": row.get("alert_message"),
            "status": "confirmed" if result else "unconfirmed",
            "status_ko": f"{result} 확인 완료" if result else UNCONFIRMED_KO,
            "guardian_result": result,
            "confirmed_at": from_iso(confirmation["confirmed_at"]) if confirmation else None,
        }
