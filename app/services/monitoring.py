from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import numpy as np

from app.config import Settings
from app.repositories import Repositories, RepositoryError
from app.services.engine import Frame, MotionEngine, build_engine
from app.timeutil import from_iso, parse_ts, to_iso
from motion_state.csi_io import N_SUB, RX_IDS
from motion_state.monitor_v2 import STATE_KO

log = logging.getLogger("carewave.monitoring")

PAGE_SIZE = 1000


class SessionNotFound(LookupError):
    pass


class SessionStopped(RuntimeError):
    pass


@dataclass
class IngestResult:
    accepted: int = 0
    rejected_invalid: int = 0
    rejected_incomplete: int = 0
    rejected_stale: int = 0
    frames: int = 0

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "rejected_invalid": self.rejected_invalid,
            "rejected_incomplete": self.rejected_incomplete,
            "rejected_stale": self.rejected_stale,
            "frames": self.frames,
        }


@dataclass(eq=False)
class Subscriber:
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop

    def push(self, message: dict) -> None:
        def put() -> None:
            if self.queue.full():
                self.queue.get_nowait()
            self.queue.put_nowait(message)

        try:
            self.loop.call_soon_threadsafe(put)
        except RuntimeError:
            pass


@dataclass
class LiveSession:
    row: dict
    engine: MotionEngine
    frames: deque
    lock: threading.RLock = field(default_factory=threading.RLock)
    pending: list = field(default_factory=list)
    last_rx_ts: dict = field(default_factory=dict)
    last_packet_ts: float | None = None
    last_receive_wall: float | None = None
    clock_offset: float | None = None
    polled_until: float | None = None
    last_flush_wall: float = 0.0
    last_status_key: tuple | None = None
    subscribers: set = field(default_factory=set)

    @property
    def id(self) -> str:
        return self.row["id"]


def _frame_row(session_id: str, f: Frame) -> dict:
    return {
        "session_id": session_id,
        "ts": to_iso(f.ts),
        "state": f.state,
        "motion_index": f.motion_index,
        "activity_score": f.activity_score,
        "candidate_sec": f.candidate_sec,
        "event_no": f.event_id,
        "signal_ok": f.signal_ok,
        "packet_rate": f.packet_rate,
        "heatmap": f.heatmap,
    }


def _row_frame(row: dict) -> dict:
    return {
        "ts": from_iso(row["ts"]),
        "state": row["state"],
        "motion_index": row.get("motion_index"),
        "activity_score": row.get("activity_score"),
        "event_id": row.get("event_no"),
        "signal_ok": row.get("signal_ok", True),
        "heatmap": row.get("heatmap"),
    }


def _mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(float(np.mean(vals)), 4) if vals else None


def downsample(frames: list[dict], max_points: int) -> tuple[list[dict], int]:
    if not frames or len(frames) <= max_points:
        return frames, 1
    size = math.ceil(len(frames) / max_points)
    out = []
    for i in range(0, len(frames), size):
        chunk = frames[i:i + size]
        rows = [c["heatmap"] for c in chunk if c.get("heatmap")]
        events = [c["event_id"] for c in chunk if c.get("event_id") is not None]
        scores = [c["activity_score"] for c in chunk if c.get("activity_score") is not None]
        out.append({
            "ts": chunk[0]["ts"],
            "state": chunk[-1]["state"],
            "motion_index": _mean(c.get("motion_index") for c in chunk),
            "activity_score": round(max(scores), 4) if scores else None,
            "event_id": events[-1] if events else None,
            "signal_ok": all(c.get("signal_ok", True) for c in chunk),
            "heatmap": [round(float(v), 2) for v in np.mean(np.array(rows, dtype=float), axis=0)] if rows else None,
        })
    return out, size


class SessionManager:
    def __init__(self, settings: Settings, repos: Repositories, model_sha256: str,
                 clock: Callable[[], float] = time.time):
        self.settings = settings
        self.repos = repos
        self.model_sha256 = model_sha256
        self.clock = clock
        self.live: dict[str, LiveSession] = {}
        self.lock = threading.RLock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.last_purge_wall = 0.0

    @property
    def buffer_size(self) -> int:
        return int(self.settings.live_retention_sec / self.settings.frame_step_sec)

    def start(self) -> tuple[dict, bool]:
        with self.lock:
            running = self.repos.sessions.list({"status": "running"}, order_by="started_at", desc=True, limit=1)
            if running:
                return self._ensure_live(running[0]).row, False
            row = self.repos.sessions.insert({
                "status": "running",
                "started_at": to_iso(self.clock()),
                "model_sha256": self.model_sha256,
            })
            self._ensure_live(row, resume=False)
            return row, True

    def stop(self, session_id: str) -> dict:
        with self.lock:
            row = self._row(session_id)
            if row["status"] == "stopped":
                self.live.pop(session_id, None)
                return row
            live = self.live.get(session_id)
            changes = {"status": "stopped", "stopped_at": to_iso(self.clock())}
            if live is not None:
                with live.lock:
                    self._accept_frames(live, live.engine.flush())
                    self._flush(live)
                    if live.last_packet_ts is not None:
                        changes["last_packet_at"] = to_iso(live.last_packet_ts)
            updated = self.repos.sessions.update(session_id, changes) or row
            if live is not None:
                self.live.pop(session_id, None)
                if live.pending:
                    log.warning("session %s stopped with %d unsaved frames", session_id, len(live.pending))
                self._publish(live, {"type": "stopped", "session_id": session_id})
            return updated

    def get(self, session_id: str) -> LiveSession | None:
        row = self._row(session_id)
        if row["status"] != "running":
            self.live.pop(session_id, None)
            return None
        return self._ensure_live(row)

    def row(self, session_id: str) -> dict:
        return self._row(session_id)

    def list(self, status: str | None = None, limit: int = 20) -> list[dict]:
        filters = {"status": status} if status else None
        return self.repos.sessions.list(filters, order_by="started_at", desc=True, limit=limit)

    def ingest(self, session_id: str, packets: list[dict]) -> IngestResult:
        live = self.get(session_id)
        if live is None:
            raise SessionStopped(session_id)
        wall = self.clock()
        result = IngestResult()
        prepared = []
        for p in packets:
            try:
                ts = parse_ts(p.get("ts"))
            except (TypeError, ValueError):
                result.rejected_invalid += 1
                continue
            rx = str(p.get("rx", "")).strip().upper()
            if rx not in RX_IDS:
                result.rejected_invalid += 1
                continue
            amp = np.array([np.nan if v is None else v for v in (p.get("amplitude") or [])], dtype=np.float64)
            if amp.shape != (N_SUB,) or not np.all(np.isfinite(amp)):
                result.rejected_incomplete += 1
                continue
            prepared.append((wall if ts is None else ts, rx, amp))
        prepared.sort(key=lambda x: x[0])
        new_frames: list[Frame] = []
        with live.lock:
            for ts, rx, amp in prepared:
                last = live.last_rx_ts.get(rx)
                if (last is not None and ts <= last) or (live.polled_until is not None and ts <= live.polled_until):
                    result.rejected_stale += 1
                    continue
                live.last_rx_ts[rx] = ts
                live.engine.push(ts, rx, amp)
                new_frames.extend(live.engine.poll(ts))
                result.accepted += 1
                live.last_packet_ts = ts if live.last_packet_ts is None else max(live.last_packet_ts, ts)
            if result.accepted:
                live.last_receive_wall = wall
                live.clock_offset = wall - live.last_packet_ts
                live.polled_until = None
            self._accept_frames(live, new_frames)
        result.frames = len(new_frames)
        return result

    def status(self, session_id: str) -> dict:
        row = self._row(session_id)
        live = self.get(session_id) if row["status"] == "running" else None
        wall = self.clock()
        started = from_iso(row["started_at"])
        stopped = from_iso(row.get("stopped_at"))
        out = {
            "id": row["id"],
            "status": row["status"],
            "started_at": started,
            "stopped_at": stopped,
            "elapsed_sec": round((stopped or wall) - started, 1),
            "last_packet_at": from_iso(row.get("last_packet_at")),
            "seconds_since_last_packet": None,
            "state": None,
            "state_ko": None,
            "motion_index": None,
            "activity_score": None,
            "rx": [{"rx": rx, "status": "none", "status_ko": "미연결", "packet_rate": 0.0, "last_packet_ts": None}
                   for rx in RX_IDS],
            "event_count": 0,
            "unconfirmed_count": 0,
        }
        if live is None:
            return out
        with live.lock:
            if live.last_receive_wall is not None:
                out["last_packet_at"] = round(live.last_packet_ts, 3)
                out["seconds_since_last_packet"] = round(max(0.0, wall - live.last_receive_wall), 1)
            out["rx"] = self._rx_status(live, wall)
            if live.frames:
                latest: Frame = live.frames[-1]
                out.update(state=latest.state, state_ko=latest.state_ko, motion_index=latest.motion_index,
                           activity_score=latest.activity_score)
            out["event_count"] = len(live.engine.events())
            out["unconfirmed_count"] = len(live.engine.open_events())
        return out

    def signals(self, session_id: str, window_sec: float | None = None, from_ts: float | None = None,
                to_ts: float | None = None, max_points: int = 1200, heatmap: bool = True) -> dict:
        row = self._row(session_id)
        live = self.get(session_id) if row["status"] == "running" else None
        events: list[dict] = []
        if live is not None:
            with live.lock:
                frames = [self._frame_point(f) for f in live.frames]
                events = live.engine.events()
                threshold = live.engine.threshold
        else:
            frames = self._stored_frames(session_id)
            threshold = None
        end = to_ts if to_ts is not None else (frames[-1]["ts"] if frames else self.clock())
        start = from_ts if from_ts is not None else end - (window_sec or self.settings.live_retention_sec)
        picked = [f for f in frames if start <= f["ts"] <= end]
        points, factor = downsample(picked, max_points)
        return {
            "session_id": session_id,
            "from_ts": round(start, 3),
            "to_ts": round(end, 3),
            "step_sec": self.settings.frame_step_sec * factor,
            "activity_threshold": None if threshold is None else round(threshold, 4),
            "count": len(points),
            "ts": [p["ts"] for p in points],
            "motion_index": [p["motion_index"] for p in points],
            "activity_score": [p["activity_score"] for p in points],
            "state": [p["state"] for p in points],
            "event_id": [p["event_id"] for p in points],
            "signal_ok": [p["signal_ok"] for p in points],
            "heatmap": [p["heatmap"] for p in points] if heatmap else None,
            "events": [e for e in events if e["start_ts"] <= end and (e["end_ts"] or end) >= start],
        }

    def snapshot(self, session_id: str, frames: int = 120) -> dict:
        live = self.get(session_id)
        recent = []
        if live is not None:
            with live.lock:
                recent = [f.to_dict() for f in list(live.frames)[-frames:]]
        return {"type": "snapshot", "session": self.status(session_id), "frames": recent}

    def subscribe(self, session_id: str, loop: asyncio.AbstractEventLoop, maxsize: int = 256) -> Subscriber:
        live = self.get(session_id)
        if live is None:
            raise SessionStopped(session_id)
        sub = Subscriber(asyncio.Queue(maxsize=maxsize), loop)
        with live.lock:
            live.subscribers.add(sub)
        return sub

    def unsubscribe(self, session_id: str, sub: Subscriber) -> None:
        live = self.live.get(session_id)
        if live is not None:
            with live.lock:
                live.subscribers.discard(sub)

    def tick(self) -> None:
        wall = self.clock()
        for live in list(self.live.values()):
            try:
                self._tick_session(live, wall)
            except Exception:
                log.exception("tick failed for session %s", live.id)
        if wall - self.last_purge_wall >= self.settings.purge_interval_sec:
            self.last_purge_wall = wall
            try:
                cutoff = to_iso(wall - self.settings.live_retention_sec)
                removed = self.repos.signal_frames.delete_where({"created_at__lt": cutoff})
                if removed:
                    log.info("purged %d live frames older than %s", removed, cutoff)
            except Exception:
                log.exception("live frame purge failed")

    def shutdown(self) -> None:
        for live in list(self.live.values()):
            with live.lock:
                self._flush(live)

    def _tick_session(self, live: LiveSession, wall: float) -> None:
        with live.lock:
            if (live.last_receive_wall is not None and wall - live.last_receive_wall >= self.settings.stall_sec):
                data_now = wall - live.clock_offset - self.settings.stall_sec
                if data_now > (live.polled_until or live.last_packet_ts):
                    frames = live.engine.poll(data_now)
                    live.polled_until = data_now
                    self._accept_frames(live, frames)
            rx = self._rx_status(live, wall)
            key = tuple(r["status"] for r in rx)
            if key != live.last_status_key:
                for r in rx:
                    self.repos.device_status.upsert({
                        "session_id": live.id,
                        "rx": r["rx"],
                        "status": r["status"],
                        "packet_rate": r["packet_rate"],
                        "last_packet_at": None if r["last_packet_ts"] is None else to_iso(r["last_packet_ts"]),
                    }, ("session_id", "rx"))
                live.last_status_key = key
            since = None if live.last_receive_wall is None else round(max(0.0, wall - live.last_receive_wall), 1)
            self._publish(live, {"type": "status", "rx": rx, "seconds_since_last_packet": since,
                                 "last_packet_at": live.last_packet_ts})
            if wall - live.last_flush_wall >= self.settings.frame_flush_sec:
                self._flush(live)
                live.last_flush_wall = wall

    def _rx_status(self, live: LiveSession, wall: float) -> list[dict]:
        if live.clock_offset is None:
            return live.engine.rx_status()
        return live.engine.rx_status(wall - live.clock_offset)

    def _accept_frames(self, live: LiveSession, frames: list[Frame]) -> None:
        if not frames:
            return
        live.frames.extend(frames)
        live.pending.extend(frames)
        overflow = len(live.pending) - self.buffer_size
        if overflow > 0:
            del live.pending[:overflow]
        self._publish(live, {"type": "frames", "frames": [f.to_dict() for f in frames]})
        for f in frames:
            if f.alert:
                self._publish(live, {"type": "alert", "ts": f.ts, "event_id": f.event_id, "message": f.alert})

    def _flush(self, live: LiveSession) -> None:
        if not live.pending:
            return
        batch = live.pending[:]
        try:
            self.repos.signal_frames.insert_many([_frame_row(live.id, f) for f in batch])
            if live.last_packet_ts is not None:
                self.repos.sessions.update(live.id, {"last_packet_at": to_iso(live.last_packet_ts)})
        except Exception:
            log.exception("frame flush failed for session %s (%d pending)", live.id, len(batch))
            return
        del live.pending[:len(batch)]

    def _publish(self, live: LiveSession, message: dict[str, Any]) -> None:
        for sub in list(live.subscribers):
            sub.push(message)

    def _frame_point(self, f: Frame) -> dict:
        return {"ts": f.ts, "state": f.state, "motion_index": f.motion_index, "activity_score": f.activity_score,
                "event_id": f.event_id, "signal_ok": f.signal_ok, "heatmap": f.heatmap}

    def _stored_frames(self, session_id: str) -> list[dict]:
        rows, offset = [], 0
        while True:
            page = self.repos.signal_frames.list({"session_id": session_id}, order_by="ts", limit=PAGE_SIZE,
                                                 offset=offset)
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return [_row_frame(r) for r in rows]

    def _row(self, session_id: str) -> dict:
        try:
            row = self.repos.sessions.get(session_id)
        except RepositoryError as exc:
            if exc.status in (400, 404):
                raise SessionNotFound(session_id) from exc
            raise
        if row is None:
            raise SessionNotFound(session_id)
        return row

    def _ensure_live(self, row: dict, resume: bool = True) -> LiveSession:
        with self.lock:
            live = self.live.get(row["id"])
            if live is None:
                live = LiveSession(row=row, engine=build_engine(self.settings),
                                   frames=deque(maxlen=self.buffer_size))
                if resume:
                    live.frames.extend(self._restore_frames(row["id"]))
                self.live[row["id"]] = live
            return live

    def _restore_frames(self, session_id: str) -> list[Frame]:
        try:
            stored = self._stored_frames(session_id)
        except Exception:
            log.exception("could not restore frames for session %s", session_id)
            return []
        return [Frame(ts=f["ts"], t=0.0, state=f["state"], state_ko=STATE_KO.get(f["state"], f["state"]),
                      motion_index=f["motion_index"], activity_score=f["activity_score"], candidate_sec=0.0,
                      event_id=f["event_id"], alert=None, signal_ok=f["signal_ok"], packet_rate={},
                      heatmap=f["heatmap"]) for f in stored]


async def run_ticker(manager: SessionManager, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(manager.tick)
        except Exception:
            log.exception("ticker iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=manager.settings.tick_sec)
        except asyncio.TimeoutError:
            pass
