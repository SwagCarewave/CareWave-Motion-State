from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from app.config import Settings
from app.repositories import Repositories, RepositoryError
from app.services.engine import RX_STATUS_KO, Frame, MotionEngine, build_engine
from app.services.series import build_series
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
    same_ts_seen: dict = field(default_factory=dict)
    last_packet_ts: float | None = None
    last_receive_wall: float | None = None
    clock_offset: float | None = None
    polled_until: float | None = None
    last_flush_wall: float = 0.0
    last_status_key: tuple | None = None
    subscribers: set = field(default_factory=set)
    retired_events: dict = field(default_factory=dict)
    saved_events: dict = field(default_factory=dict)
    alert_messages: dict = field(default_factory=dict)
    restored_rx: list = field(default_factory=list)
    event_uuids: dict = field(default_factory=dict)
    saved_last_packet: float | None = None

    @property
    def id(self) -> str:
        return self.row["id"]


def _frame_row(session_id: str, frame_id: str, f: Frame) -> dict:
    return {
        "id": frame_id,
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


def _event_row(session_id: str, event_uuid: str, e: dict, message: str | None) -> dict:
    return {
        "id": event_uuid,
        "session_id": session_id,
        "analysis_id": None,
        "event_no": e["id"],
        "started_at": to_iso(e["start_ts"]),
        "alerted_at": to_iso(e["alert_ts"]),
        "ended_at": None if e["end_ts"] is None else to_iso(e["end_ts"]),
        "duration_sec": e["duration_sec"],
        "alert_message": message,
    }


def _row_event(row: dict, guardian: str | None) -> dict:
    return {
        "id": row["event_no"],
        "uuid": row["id"],
        "start_ts": from_iso(row["started_at"]),
        "alert_ts": from_iso(row["alerted_at"]),
        "end_ts": from_iso(row.get("ended_at")),
        "duration_sec": row.get("duration_sec") or 0.0,
        "ongoing": False,
        "guardian_result": guardian,
    }


def _event_key(e: dict) -> tuple:
    return e["start_ts"], e["alert_ts"], e["end_ts"], e["duration_sec"]


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
                    try:
                        self._save_rx(live, self.clock(), force=True)
                    except Exception:
                        log.exception("final receiver state save failed for session %s", session_id)
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
            ts = wall if ts is None else ts
            if not math.isfinite(ts) or ts > wall + self.settings.future_skew_sec:
                result.rejected_invalid += 1
                continue
            prepared.append((ts, rx, amp))
        prepared.sort(key=lambda x: x[0])
        new_frames: list[Frame] = []
        with live.lock:
            for ts, rx, amp in prepared:
                last = live.last_rx_ts.get(rx)
                digest = amp.tobytes()
                if ((last is not None and (ts < last or (ts == last and digest in live.same_ts_seen[rx])))
                        or (live.polled_until is not None and ts <= live.polled_until)):
                    result.rejected_stale += 1
                    continue
                if live.last_packet_ts is not None and ts - live.last_packet_ts > self.settings.max_gap_sec:
                    new_frames.extend(self._new_segment(live))
                if ts != last:
                    live.same_ts_seen[rx] = set()
                live.same_ts_seen[rx].add(digest)
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
            "rx": [self._rx_entry(rx, "none", 0.0, None) for rx in RX_IDS],
            "event_count": 0,
            "unconfirmed_count": 0,
        }
        if live is None:
            out["rx"] = self._stored_rx(session_id) or out["rx"]
            latest = self._latest_stored_frame(session_id)
            if latest is not None:
                out.update(state=latest["state"], state_ko=STATE_KO.get(latest["state"], latest["state"]),
                           motion_index=latest["motion_index"], activity_score=latest["activity_score"])
        else:
            with live.lock:
                if live.last_receive_wall is not None:
                    out["last_packet_at"] = round(live.last_packet_ts, 3)
                    out["seconds_since_last_packet"] = round(max(0.0, wall - live.last_receive_wall), 1)
                out["rx"] = self._rx_status(live, wall)
                if live.frames:
                    latest_frame: Frame = live.frames[-1]
                    out.update(state=latest_frame.state, state_ko=latest_frame.state_ko,
                               motion_index=latest_frame.motion_index, activity_score=latest_frame.activity_score)
        events = self.events(session_id, live)
        out["event_count"] = len(events)
        out["unconfirmed_count"] = sum(1 for e in events if e["guardian_result"] is None)
        if live is not None and out["state"] == "low_motion" and out["unconfirmed_count"]:
            out.update(state="awaiting_confirmation", state_ko=STATE_KO["awaiting_confirmation"])
        elif live is not None and out["state"] == "awaiting_confirmation" and not out["unconfirmed_count"]:
            out.update(state="low_motion", state_ko=STATE_KO["low_motion"])
        return out

    def live_event_state(self, session_id: str) -> dict[int, dict]:
        live = self.live.get(session_id)
        if live is None:
            return {}
        with live.lock:
            return {e["id"]: e for e in self._live_events(live)}

    def find_live_event(self, event_uuid: str) -> dict | None:
        for live in list(self.live.values()):
            with live.lock:
                number = next((n for n, u in live.event_uuids.items() if u == event_uuid), None)
                if number is None:
                    continue
                e = next((x for x in self._live_events(live) if x["id"] == number), None)
                if e is None:
                    continue
                row = _event_row(live.id, event_uuid, e, live.alert_messages.get(number))
                row["created_at"] = row["started_at"]
                return row
        return None

    def close_live_event(self, session_id: str, number: int, result: str) -> None:
        live = self.live.get(session_id)
        if live is None:
            return
        with live.lock:
            try:
                live.engine.close_event(number, result)
            except KeyError:
                pass
            if number in live.retired_events:
                live.retired_events[number] = {**live.retired_events[number], "guardian_result": result}
            self._publish(live, {"type": "event", "id": live.event_uuids.get(number), "number": number})

    def events(self, session_id: str, live: LiveSession | None = None) -> list[dict]:
        merged: dict[int, dict] = {}
        try:
            rows = self.repos.activity_events.list({"session_id": session_id}, order_by="event_no")
            confirmations = {}
            if rows:
                found = self.repos.guardian_confirmations.list({"event_id__in": [r["id"] for r in rows]})
                confirmations = {c["event_id"]: c["result"] for c in found}
            merged = {r["event_no"]: _row_event(r, confirmations.get(r["id"])) for r in rows}
        except Exception:
            if live is None:
                raise
            log.exception("could not load stored events for session %s", session_id)
        if live is not None:
            with live.lock:
                for e in self._live_events(live):
                    stored = merged.get(e["id"])
                    e = {**e, "uuid": live.event_uuids.get(e["id"])}
                    if stored is not None and e["guardian_result"] is None:
                        e["guardian_result"] = stored["guardian_result"]
                    merged[e["id"]] = e
        return [merged[k] for k in sorted(merged)]

    def signals(self, session_id: str, window_sec: float | None = None, from_ts: float | None = None,
                to_ts: float | None = None, max_points: int = 1200, heatmap: bool = True) -> dict:
        row = self._row(session_id)
        live = self.get(session_id) if row["status"] == "running" else None
        if live is not None:
            with live.lock:
                frames = [self._frame_point(f) for f in live.frames]
                threshold = live.engine.threshold
        else:
            frames = self._stored_frames(session_id)
            threshold = None
        events = self.events(session_id, live)
        end = to_ts if to_ts is not None else (frames[-1]["ts"] if frames else self.clock())
        start = from_ts if from_ts is not None else end - (window_sec or self.settings.live_retention_sec)
        series = build_series(frames, events, start, end, max_points, heatmap, threshold, self.settings.frame_step_sec,
                              self.settings.event_burst_sec)
        return {"session_id": session_id, **series}

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
            rx = self._save_rx(live, wall)
            since = None if live.last_receive_wall is None else round(max(0.0, wall - live.last_receive_wall), 1)
            self._publish(live, {"type": "status", "rx": rx, "seconds_since_last_packet": since,
                                 "last_packet_at": live.last_packet_ts})
            if wall - live.last_flush_wall >= self.settings.frame_flush_sec:
                self._flush(live)
                live.last_flush_wall = wall

    def _save_rx(self, live: LiveSession, wall: float, force: bool = False) -> list[dict]:
        rx = self._rx_status(live, wall)
        key = tuple(r["status"] for r in rx)
        if force or key != live.last_status_key:
            for r in rx:
                self.repos.device_status.upsert({
                    "session_id": live.id,
                    "rx": r["rx"],
                    "status": r["status"],
                    "packet_rate": r["packet_rate"],
                    "last_packet_at": None if r["last_packet_ts"] is None else to_iso(r["last_packet_ts"]),
                }, ("session_id", "rx"))
            live.last_status_key = key
        return rx

    @staticmethod
    def _rx_entry(rx: str, status: str, rate: float, last_ts: float | None) -> dict:
        return {"rx": rx, "status": status, "status_ko": RX_STATUS_KO[status], "packet_rate": rate,
                "last_packet_ts": last_ts}

    def _rx_status(self, live: LiveSession, wall: float) -> list[dict]:
        if live.clock_offset is not None:
            return live.engine.rx_status(wall - live.clock_offset)
        if not live.restored_rx:
            return live.engine.rx_status()
        out = []
        for r in live.restored_rx:
            last = r["last_packet_ts"]
            status = "lost" if last is not None and wall - last > self.settings.rx_lost_sec else r["status"]
            out.append(self._rx_entry(r["rx"], status, 0.0 if status == "lost" else r["packet_rate"], last))
        return out

    def _stored_rx(self, session_id: str) -> list[dict]:
        rows = {r["rx"]: r for r in self.repos.device_status.list({"session_id": session_id})}
        if not rows:
            return []
        return [self._rx_entry(rx, rows[rx]["status"], float(rows[rx].get("packet_rate") or 0.0),
                               from_iso(rows[rx].get("last_packet_at"))) if rx in rows
                else self._rx_entry(rx, "none", 0.0, None) for rx in RX_IDS]

    def _latest_stored_frame(self, session_id: str) -> dict | None:
        rows = self.repos.signal_frames.list({"session_id": session_id}, order_by="ts", desc=True, limit=1)
        return _row_frame(rows[0]) if rows else None

    def _live_events(self, live: LiveSession) -> list[dict]:
        merged = dict(live.retired_events)
        merged.update({e["id"]: e for e in live.engine.events()})
        return [merged[k] for k in sorted(merged)]

    def _new_segment(self, live: LiveSession) -> list[Frame]:
        tail = live.engine.flush()
        for e in live.engine.events():
            live.retired_events[e["id"]] = {**e, "ongoing": False}
        known = [*live.retired_events, *live.saved_events]
        live.engine = build_engine(self.settings)
        live.engine.continue_event_numbers(max(known, default=0) + 1)
        live.clock_offset = None
        log.info("session %s: timestamp gap over %.0fs, started a new engine segment", live.id,
                 self.settings.max_gap_sec)
        return tail

    def _accept_frames(self, live: LiveSession, frames: list[Frame]) -> None:
        if not frames:
            return
        live.frames.extend(frames)
        live.pending.extend((str(uuid.uuid4()), f) for f in frames)
        overflow = len(live.pending) - self.buffer_size
        if overflow > 0:
            del live.pending[:overflow]
        self._publish(live, {"type": "frames", "frames": [f.to_dict() for f in frames]})
        alerts = [f for f in frames if f.alert]
        if not alerts:
            return
        for f in alerts:
            live.alert_messages[f.event_id] = f.alert
            live.event_uuids.setdefault(f.event_id, str(uuid.uuid4()))
        try:
            self._persist_events(live)
        except Exception:
            log.exception("event save before alert failed for session %s", live.id)
        for f in alerts:
            self._publish(live, {"type": "alert", "ts": f.ts, "event_id": f.event_id,
                                 "event_uuid": live.event_uuids[f.event_id], "message": f.alert})

    def _flush(self, live: LiveSession) -> None:
        if live.pending:
            batch = live.pending[:]
            try:
                self.repos.signal_frames.insert_many([_frame_row(live.id, fid, f) for fid, f in batch])
            except Exception:
                log.exception("frame flush failed for session %s (%d pending)", live.id, len(batch))
            else:
                del live.pending[:len(batch)]
        try:
            self._persist_events(live)
        except Exception:
            log.exception("event save failed for session %s", live.id)
        if live.last_packet_ts is not None and live.last_packet_ts != live.saved_last_packet:
            try:
                self.repos.sessions.update(live.id, {"last_packet_at": to_iso(live.last_packet_ts)})
                live.saved_last_packet = live.last_packet_ts
            except Exception:
                log.exception("session update failed for session %s", live.id)

    def _persist_events(self, live: LiveSession) -> None:
        for e in self._live_events(live):
            key = _event_key(e)
            if live.saved_events.get(e["id"]) == key:
                continue
            event_uuid = live.event_uuids.setdefault(e["id"], str(uuid.uuid4()))
            self.repos.activity_events.upsert(_event_row(live.id, event_uuid, e, live.alert_messages.get(e["id"])),
                                              ("session_id", "analysis_id", "event_no"))
            live.saved_events[e["id"]] = key
            self._publish(live, {"type": "event", "id": event_uuid, "number": e["id"]})

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
                    self._resume(live)
                self.live[row["id"]] = live
            return live

    def _resume(self, live: LiveSession) -> None:
        live.frames.extend(self._restore_frames(live.id))
        last_packet = from_iso(live.row.get("last_packet_at"))
        marks = [t for t in (last_packet, live.frames[-1].ts if live.frames else None) if t is not None]
        if marks:
            live.polled_until = max(marks)
            live.last_packet_ts = last_packet
            live.saved_last_packet = last_packet
        try:
            stored = self.repos.activity_events.list({"session_id": live.id})
            live.saved_events = {r["event_no"]: _event_key(_row_event(r, None)) for r in stored}
            live.alert_messages = {r["event_no"]: r.get("alert_message") for r in stored}
            live.event_uuids = {r["event_no"]: r["id"] for r in stored}
            live.restored_rx = self._stored_rx(live.id)
        except Exception:
            log.exception("could not restore events or receiver state for session %s", live.id)
        live.engine.continue_event_numbers(max(live.saved_events, default=0) + 1)
        log.info("resumed session %s: %d frames, %d events, packets accepted after %s", live.id, len(live.frames),
                 len(live.saved_events), live.polled_until)

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
