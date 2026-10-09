from __future__ import annotations

import asyncio
import bisect
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from app.config import Settings
from app.services.analysis import AnalysisService
from app.services.exceptions import ServiceError, not_found
from app.services.monitoring import Subscriber

log = logging.getLogger("carewave.playback")

SPEEDS = (1, 4, 16)
CURSOR_EPS = 1e-6


@dataclass
class Replay:
    id: str
    analysis_id: str
    file_id: str
    record_start: float
    record_end: float
    from_ts: float
    to_ts: float
    speed: int
    playing: bool
    anchor_pos: float
    anchor_wall: float
    last_access: float
    sent_until: float | None = None
    subscribers: set = field(default_factory=set)


class ReplayManager:
    def __init__(self, settings: Settings, analyses: AnalysisService, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.analyses = analyses
        self.clock = clock
        self.replays: dict[str, Replay] = {}
        self.lock = threading.RLock()
        analyses.files.on_delete.append(self._on_file_delete)
        analyses.on_retire.append(self._remove_for)

    def create(self, analysis_id: str, from_ts: float | None = None, to_ts: float | None = None, speed: int = 1,
               position_ts: float | None = None, play: bool = False) -> dict:
        meta, points = self.analyses.points(analysis_id)
        start, end = meta["record_start"], meta["record_end"]
        if points:
            start, end = min(start, points[0]["ts"]), max(end, points[-1]["ts"])
        lo, hi = self._range(start, end, from_ts, to_ts)
        now = self.clock()
        replay = Replay(id=str(uuid.uuid4()), analysis_id=analysis_id, file_id=meta.get("file_id"),
                        record_start=start, record_end=end, from_ts=lo, to_ts=hi, speed=self._speed(speed),
                        playing=False, anchor_pos=self._clamp(position_ts if position_ts is not None else lo, lo, hi),
                        anchor_wall=now, last_access=now)
        replay.sent_until = replay.anchor_pos - CURSOR_EPS
        if play:
            self._play(replay, now)
        with self.lock:
            self.replays[replay.id] = replay
        return self.state(replay.id)

    def state(self, replay_id: str) -> dict:
        replay = self._get(replay_id)
        now = self.clock()
        with self.lock:
            replay.last_access = now
            return self._view(replay, now)

    def update(self, replay_id: str, action: str | None = None, position_ts: float | None = None,
               speed: int | None = None, from_ts: float | None = None, to_ts: float | None = None) -> dict:
        replay = self._get(replay_id)
        now = self.clock()
        with self.lock:
            pos = self._position(replay, now)
            replay.anchor_pos, replay.anchor_wall = pos, now
            if from_ts is not None or to_ts is not None:
                replay.from_ts, replay.to_ts = self._range(replay.record_start, replay.record_end,
                                                           from_ts if from_ts is not None else replay.from_ts,
                                                           to_ts if to_ts is not None else replay.to_ts)
                replay.anchor_pos = self._clamp(replay.anchor_pos, replay.from_ts, replay.to_ts)
                replay.sent_until = replay.anchor_pos - CURSOR_EPS
            if speed is not None:
                replay.speed = self._speed(speed)
            if position_ts is not None:
                replay.anchor_pos = self._clamp(position_ts, replay.from_ts, replay.to_ts)
                replay.sent_until = replay.anchor_pos - CURSOR_EPS
            if action == "play":
                self._play(replay, now)
            elif action == "pause":
                replay.playing = False
            replay.last_access = now
            view = self._view(replay, now)
        self._publish(replay, {"type": "state", "replay": view})
        return view

    def subscribe(self, replay_id: str, loop: asyncio.AbstractEventLoop, maxsize: int = 256) -> Subscriber:
        replay = self._get(replay_id)
        sub = Subscriber(asyncio.Queue(maxsize=maxsize), loop)
        with self.lock:
            replay.subscribers.add(sub)
        return sub

    def unsubscribe(self, replay_id: str, sub: Subscriber) -> None:
        with self.lock:
            replay = self.replays.get(replay_id)
            if replay is not None:
                replay.subscribers.discard(sub)

    def snapshot(self, replay_id: str) -> dict:
        return {"type": "state", "replay": self.state(replay_id)}

    def tick(self) -> None:
        now = self.clock()
        with self.lock:
            replays = list(self.replays.values())
        for replay in replays:
            try:
                self._tick_one(replay, now)
            except Exception:
                log.exception("replay tick failed for %s", replay.id)

    def _tick_one(self, replay: Replay, now: float) -> None:
        with self.lock:
            if not replay.subscribers and now - replay.last_access > self.settings.replay_idle_sec:
                self.replays.pop(replay.id, None)
                return
            if not replay.subscribers:
                return
            view = self._view(replay, now)
            pos = view["position_ts"]
            since = replay.sent_until if replay.sent_until is not None else pos
            replay.sent_until = pos
            if view["status"] != "playing" and since == pos:
                return
        try:
            _, points = self.analyses.points(replay.analysis_id)
        except ServiceError:
            return
        frames = []
        if pos > since:
            times = [p["ts"] for p in points]
            frames = points[bisect.bisect_right(times, since):bisect.bisect_right(times, pos)]
        self._publish(replay, {"type": "tick", "replay": view, "frames": frames})

    def _on_file_delete(self, file_row: dict, analyses: list[dict]) -> None:
        self._remove_for({a["id"] for a in analyses})

    def _remove_for(self, ids: set[str]) -> None:
        with self.lock:
            doomed = [r for r in self.replays.values() if r.analysis_id in ids]
            for r in doomed:
                self.replays.pop(r.id, None)
        for r in doomed:
            self._publish(r, {"type": "removed", "replay_id": r.id})

    def _get(self, replay_id: str) -> Replay:
        with self.lock:
            replay = self.replays.get(replay_id)
        if replay is None:
            raise not_found("replay_not_found", "재생 정보를 찾을 수 없습니다. 재생을 다시 시작해 주세요.", "replay_id", replay_id)
        return replay

    def _view(self, replay: Replay, now: float) -> dict:
        pos = self._position(replay, now)
        ended = pos >= replay.to_ts
        if ended and replay.playing:
            replay.playing = False
            replay.anchor_pos, replay.anchor_wall = replay.to_ts, now
        status = "playing" if replay.playing else ("ended" if ended else "paused")
        span = replay.to_ts - replay.from_ts
        return {
            "id": replay.id,
            "analysis_id": replay.analysis_id,
            "file_id": replay.file_id,
            "status": status,
            "position_ts": round(pos, 3),
            "speed": replay.speed,
            "from_ts": round(replay.from_ts, 3),
            "to_ts": round(replay.to_ts, 3),
            "record_start": round(replay.record_start, 3),
            "record_end": round(replay.record_end, 3),
            "progress": round((pos - replay.from_ts) / span, 4) if span > 0 else 1.0,
        }

    def _play(self, replay: Replay, now: float) -> None:
        if replay.anchor_pos >= replay.to_ts:
            replay.anchor_pos = replay.from_ts
            replay.sent_until = replay.from_ts - CURSOR_EPS
        replay.playing = True
        replay.anchor_wall = now

    @staticmethod
    def _position(replay: Replay, now: float) -> float:
        if not replay.playing:
            return replay.anchor_pos
        return min(replay.to_ts, replay.anchor_pos + (now - replay.anchor_wall) * replay.speed)

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    @staticmethod
    def _speed(speed: int) -> int:
        if speed not in SPEEDS:
            raise ServiceError(422, "invalid_speed", "배속은 1, 4, 16 중 하나여야 합니다.", {"allowed": list(SPEEDS)})
        return speed

    def _range(self, start: float, end: float, from_ts: float | None, to_ts: float | None) -> tuple[float, float]:
        lo = self._clamp(from_ts if from_ts is not None else start, start, end)
        hi = self._clamp(to_ts if to_ts is not None else end, start, end)
        if hi <= lo:
            raise ServiceError(422, "invalid_range", "재생 구간의 끝이 시작보다 뒤여야 합니다.",
                               {"from_ts": from_ts, "to_ts": to_ts, "record_start": start, "record_end": end})
        return lo, hi

    def _publish(self, replay: Replay, message: dict) -> None:
        for sub in list(replay.subscribers):
            sub.push(message)


async def run_replay_ticker(manager: ReplayManager, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(manager.tick)
        except Exception:
            log.exception("replay ticker iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=manager.settings.replay_tick_sec)
        except asyncio.TimeoutError:
            pass
