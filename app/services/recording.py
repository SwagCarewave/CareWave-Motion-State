from __future__ import annotations

import csv
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

import numpy as np

from app.config import Settings
from app.services.exceptions import ServiceError
from motion_state.csi_io import SUB_COLS

log = logging.getLogger("carewave.recording")

KST = timezone(timedelta(hours=9))
HEADER = ["experiment_id", "timestamp", "label", "rx", *SUB_COLS]
LABEL = "unlabeled"

UNAVAILABLE = {
    "none": "이 세션의 녹화가 없습니다. 취침 모드를 새로 시작하면 녹화됩니다.",
    "recording": "아직 녹화 중입니다. 취침 모드를 끈 뒤 받을 수 있습니다.",
    "too_long": "녹화 시간이 최대 길이를 넘어 CSV를 만들지 않았습니다.",
    "empty": "녹화된 CSI 패킷이 없습니다.",
    "expired": "보관 시간이 지나 녹화가 삭제되었습니다.",
    "failed": "녹화 파일을 저장하지 못했습니다.",
}


@dataclass
class Recording:
    session_id: str
    path: Path
    experiment_id: str
    handle: TextIO | None
    writer: Any
    first_ts: float | None = None
    last_ts: float | None = None
    packets: int = 0
    too_long: bool = False
    failed: bool = False
    deleted: bool = False
    stopped_wall: float | None = None

    @property
    def status(self) -> str:
        if self.failed:
            return "failed"
        if self.too_long:
            return "too_long"
        if self.stopped_wall is None:
            return "recording"
        if self.deleted:
            return "expired"
        return "ready" if self.packets else "empty"


class RecordingService:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.clock = clock
        self.dir = settings.recordings_path
        self.lock = threading.Lock()
        self.current: Recording | None = None
        self._clear_dir()

    def begin(self, session_id: str) -> None:
        with self.lock:
            if self.current is not None:
                self._delete(self.current)
                self.current = None
            self._clear_dir()
            stamp = datetime.fromtimestamp(self.clock(), KST).strftime("%Y%m%d_%H%M%S")
            path = self.dir / f"{session_id}.csv"
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                handle = path.open("w", newline="", encoding="utf-8")
                writer = csv.writer(handle)
                writer.writerow(HEADER)
            except OSError:
                log.exception("could not open raw CSI recording for session %s", session_id)
                self.current = Recording(session_id, path, stamp, None, None, failed=True)
                return
            self.current = Recording(session_id, path, stamp, handle, writer)

    def write(self, session_id: str, packets: list[tuple[float, str, np.ndarray]]) -> None:
        if not packets:
            return
        with self.lock:
            rec = self.current
            if rec is None or rec.session_id != session_id or rec.handle is None:
                return
            limit = self.settings.recording_max_sec
            try:
                for ts, rx, amp in packets:
                    if rec.first_ts is None:
                        rec.first_ts = ts
                    if ts - rec.first_ts > limit:
                        rec.too_long = True
                        self._delete(rec)
                        log.info("raw CSI recording for session %s passed %ss and was dropped", session_id, limit)
                        return
                    rec.writer.writerow([rec.experiment_id, datetime.fromtimestamp(ts, KST).isoformat(), LABEL, rx,
                                         *(round(float(a), 6) for a in amp)])
                    rec.packets += 1
                    rec.last_ts = ts if rec.last_ts is None else max(rec.last_ts, ts)
            except OSError:
                log.exception("raw CSI recording write failed for session %s", session_id)
                rec.failed = True
                self._delete(rec)

    def finish(self, session_id: str) -> None:
        with self.lock:
            rec = self.current
            if rec is None or rec.session_id != session_id or rec.stopped_wall is not None:
                return
            rec.stopped_wall = self.clock()
            if rec.handle is not None:
                try:
                    rec.handle.close()
                except OSError:
                    log.exception("could not close raw CSI recording for session %s", session_id)
                    rec.failed = True
                    self._delete(rec)
                rec.handle = None

    def purge(self) -> None:
        with self.lock:
            self._expire()

    def status(self, session_id: str) -> dict:
        with self.lock:
            self._expire()
            rec = self.current if self.current is not None and self.current.session_id == session_id else None
            return self._view(session_id, rec)

    def download(self, session_id: str) -> tuple[str, bytes]:
        with self.lock:
            self._expire()
            rec = self.current if self.current is not None and self.current.session_id == session_id else None
            status = "none" if rec is None else rec.status
            if status != "ready":
                raise ServiceError(409, "recording_unavailable", UNAVAILABLE[status],
                                   {"session_id": session_id, "status": status})
            try:
                data = rec.path.read_bytes()
            except OSError:
                log.exception("raw CSI recording for session %s could not be read", session_id)
                rec.failed = True
                self._delete(rec)
                raise ServiceError(409, "recording_unavailable", UNAVAILABLE["failed"],
                                   {"session_id": session_id, "status": "failed"})
            return f"csi_raw_live_{rec.experiment_id}.csv", data

    def _view(self, session_id: str, rec: Recording | None) -> dict:
        keep, limit = self.settings.recording_keep_sec, self.settings.recording_max_sec
        if rec is None:
            return {"session_id": session_id, "status": "none", "downloadable": False, "packets": 0,
                    "duration_sec": 0.0, "size_bytes": None, "started_at": None, "ended_at": None,
                    "expires_at": None, "max_duration_sec": limit, "keep_sec": keep}
        status = rec.status
        size = None
        if status == "ready":
            try:
                size = rec.path.stat().st_size
            except OSError:
                size = None
        span = 0.0 if rec.first_ts is None or rec.last_ts is None else round(rec.last_ts - rec.first_ts, 3)
        return {
            "session_id": session_id,
            "status": status,
            "downloadable": status == "ready",
            "packets": rec.packets,
            "duration_sec": span,
            "size_bytes": size,
            "started_at": rec.first_ts,
            "ended_at": rec.last_ts,
            "expires_at": None if status != "ready" else round(rec.stopped_wall + keep, 3),
            "max_duration_sec": limit,
            "keep_sec": keep,
        }

    def _expire(self) -> None:
        rec = self.current
        if rec is None or rec.stopped_wall is None or rec.deleted:
            return
        if self.clock() - rec.stopped_wall >= self.settings.recording_keep_sec:
            self._delete(rec)
            log.info("raw CSI recording for session %s expired", rec.session_id)

    def _delete(self, rec: Recording) -> None:
        if rec.handle is not None:
            try:
                rec.handle.close()
            except OSError:
                pass
            rec.handle = None
        try:
            rec.path.unlink(missing_ok=True)
        except OSError:
            log.exception("could not delete raw CSI recording %s", rec.path)
        rec.deleted = True

    def _clear_dir(self) -> None:
        if not self.dir.is_dir():
            return
        for path in self.dir.glob("*.csv"):
            try:
                path.unlink()
            except OSError:
                log.exception("could not delete leftover recording %s", path)
