from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import Counter, deque
from pathlib import Path
from typing import Callable

import torch

from app.pose.output import ACTION_KO, DIRECTION_KO, POSTURE_KO
from app.pose.packets import LinkRecord
from app.pose.registry import PoseModelError, list_models, load_runtime
from app.services.monitoring import Subscriber

log = logging.getLogger("carewave.pose")

LINK_STATUS_KO = {"ok": "정상", "weak": "신호 부족", "lost": "연결 끊김", "none": "미연결"}


class PoseModelNotFound(LookupError):
    pass


class PoseService:
    def __init__(self, models_dir: Path, active: str, rate_window_sec: float = 2.0, weak_hz: float = 20.0,
                 lost_sec: float = 2.0, poll_margin_sec: float = 0.1, clock: Callable[[], float] = time.time):
        self.models_dir = Path(models_dir)
        self.rate_window_sec = rate_window_sec
        self.weak_hz = weak_hz
        self.lost_sec = lost_sec
        self.poll_margin_sec = poll_margin_sec
        self.clock = clock
        self.lock = threading.Lock()
        self.inbox: list[tuple[float, list[LinkRecord]]] = []
        self.inbox_lock = threading.Lock()
        self.subscribers: set[Subscriber] = set()
        self.stats: Counter = Counter()
        self.recent: deque = deque(maxlen=50)
        self.latest: dict | None = None
        self.last_record_wall: float | None = None
        self.runtime = self._load(active)
        self._reset_links()

    def _load(self, name: str):
        directory = self.models_dir / name
        if not (directory / "manifest.json").is_file():
            raise PoseModelNotFound(name)
        return load_runtime(directory)

    def _reset_links(self) -> None:
        self.link_times = {k: deque() for k in self.runtime.links}
        self.link_last: dict[str, float | None] = {k: None for k in self.runtime.links}

    def models(self) -> list[dict]:
        out = list_models(self.models_dir)
        for m in out:
            m["active"] = m["name"] == self.runtime.name
        return out

    def switch(self, name: str) -> dict:
        runtime = self._load(name)
        with self.lock:
            self.runtime = runtime
            self._reset_links()
            self.latest = None
            self.recent.clear()
        log.info("pose model switched to %s", name)
        info = self.model_info()
        self._publish({"type": "model", "model": info})
        return info

    def recalibrate(self) -> dict:
        with self.lock:
            self.runtime.reset()
            self.latest = None
            self.recent.clear()
        info = self.calibration()
        self._publish({"type": "calibration", "calibration": info})
        return info

    def calibration(self) -> dict:
        return self.runtime.calibration() or {"required": False, "active": False, "progress": 1.0, "seconds": 0.0}

    def model_info(self) -> dict:
        m = self.runtime.manifest
        return {
            "name": m["name"],
            "type": m.get("type"),
            "title": m.get("title", m["name"]),
            "points": m["points"],
            "skeleton": m.get("skeleton", []),
            "classes": m["classes"],
            "links": m["links"],
            "image_size": m.get("image_size"),
            "display": m.get("display", {}),
            "game": m.get("game", {}),
            "fall": m.get("fall", {}),
            "labels": {"action": {c: ACTION_KO.get(c, c) for c in m["classes"]}, "posture": POSTURE_KO,
                       "direction": DIRECTION_KO, "link_status": LINK_STATUS_KO},
        }

    def ingest(self, ts: float, records: list[LinkRecord]) -> None:
        if not records:
            return
        with self.inbox_lock:
            self.inbox.append((ts, records))
            if len(self.inbox) > 20000:
                self.inbox.pop(0)
                self.stats["dropped_overflow"] += 1

    def tick(self) -> None:
        now = self.clock()
        with self.inbox_lock:
            batch, self.inbox = self.inbox, []
        frames: list[dict] = []
        with self.lock:
            runtime = self.runtime
            was_calibrating = self.calibration()["active"]
            for ts, records in batch:
                frames.extend(runtime.poll(ts - self.poll_margin_sec))
                self.last_record_wall = ts
                for r in records:
                    if runtime.add(ts, r):
                        self.stats["records"] += 1
                        self.link_times[r.link].append(ts)
                        self.link_last[r.link] = ts
                    else:
                        self.stats["rejected_unknown_link"] += 1
            frames.extend(runtime.poll(now - self.poll_margin_sec))
            for f in frames:
                self.latest = f
                self.recent.append(f)
            calibration = self.calibration()
        if was_calibrating and not calibration["active"]:
            self._publish({"type": "calibration", "calibration": calibration})
        for f in frames:
            self._publish(f)

    def publish_links(self) -> None:
        self._publish({"type": "links", **self.links()})

    def links(self) -> dict:
        now = self.clock()
        out = []
        with self.lock:
            for key in self.runtime.links:
                q = self.link_times[key]
                while q and q[0] < now - self.rate_window_sec:
                    q.popleft()
                last = self.link_last[key]
                rate = len(q) / self.rate_window_sec
                if last is None:
                    code = "none"
                elif now - last > self.lost_sec:
                    code = "lost"
                elif rate < self.weak_hz:
                    code = "weak"
                else:
                    code = "ok"
                receiver, sender = key[1:].split("_p")
                out.append({"link": key, "from": int(sender), "to": int(receiver), "status": code,
                            "status_ko": LINK_STATUS_KO[code], "rate_hz": round(rate, 1),
                            "age_sec": None if last is None else round(max(0.0, now - last), 2)})
            quality = self.runtime.quality()
            calibration = self.calibration()
        ok = sum(1 for l in out if l["status"] == "ok")
        since = None if self.last_record_wall is None else round(max(0.0, now - self.last_record_wall), 1)
        return {"links": out, "links_ok": ok, "links_total": len(out), "ready": ok == len(out),
                "valid_frame_ratio": quality.get("valid_frame_ratio"), "calibration": calibration,
                "seconds_since_last_record": since}

    def status(self) -> dict:
        return {"model": self.model_info(), "latest": self.latest, "counters": dict(self.stats),
                "predictions": self.runtime.predictions, **self.links()}

    def snapshot(self) -> dict:
        return {"type": "snapshot", "model": self.model_info(), "latest": self.latest,
                "recent": list(self.recent), **self.links()}

    def subscribe(self, loop: asyncio.AbstractEventLoop, maxsize: int = 256) -> Subscriber:
        sub = Subscriber(asyncio.Queue(maxsize=maxsize), loop)
        with self.lock:
            self.subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self.lock:
            self.subscribers.discard(sub)

    def _publish(self, message: dict) -> None:
        with self.lock:
            subs = list(self.subscribers)
        for s in subs:
            s.push(message)


def build_pose_service(models_dir: Path, active: str, threads: int = 1) -> PoseService | None:
    torch.set_num_threads(threads)
    try:
        return PoseService(models_dir, active)
    except (PoseModelError, PoseModelNotFound, FileNotFoundError):
        log.exception("pose model %s could not be loaded; 6-node pose stream is off", active)
        return None


async def run_pose_ticker(service: PoseService, stop: asyncio.Event, tick_sec: float = 0.1,
                          links_every_sec: float = 1.0) -> None:
    last_links = 0.0
    while not stop.is_set():
        try:
            await asyncio.to_thread(service.tick)
            now = time.monotonic()
            if now - last_links >= links_every_sec:
                last_links = now
                service.publish_links()
        except Exception:
            log.exception("pose tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_sec)
        except asyncio.TimeoutError:
            pass
