from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from motion_state.csi_io import N_SUB, RX_IDS
from motion_state.monitor_v2 import GUARDIAN_RESULTS, MonitorOutput, NightMonitorV2, ServiceConfig

RX_STATUS_KO = {"ok": "정상", "weak": "신호 부족", "lost": "연결 끊김", "none": "미연결"}


@dataclass(frozen=True)
class RxPolicy:
    weak_rate: float = 3.0
    rate_window_sec: float = 2.0
    lost_sec: float = 5.0


@dataclass
class Frame:
    ts: float
    t: float
    state: str
    state_ko: str
    motion_index: float | None
    activity_score: float | None
    candidate_sec: float
    event_id: int | None
    alert: str | None
    signal_ok: bool
    packet_rate: dict
    heatmap: list[float] | None

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "t": self.t,
            "state": self.state,
            "state_ko": self.state_ko,
            "motion_index": self.motion_index,
            "activity_score": self.activity_score,
            "candidate_sec": self.candidate_sec,
            "event_id": self.event_id,
            "alert": self.alert,
            "signal_ok": self.signal_ok,
            "packet_rate": self.packet_rate,
            "heatmap": self.heatmap,
        }


@dataclass
class _HeatBin:
    total: np.ndarray = field(default_factory=lambda: np.zeros(N_SUB))
    count: np.ndarray = field(default_factory=lambda: np.zeros(N_SUB))


class MotionEngine:
    def __init__(self, model_path: Path, origin_ts: float | None = None, service: ServiceConfig | None = None,
                 rx_policy: RxPolicy | None = None, baseline: float | None = None, step_sec: float = 0.5):
        self.monitor = NightMonitorV2(model_path, service or ServiceConfig(), baseline)
        self.rx_policy = rx_policy or RxPolicy()
        self.step_sec = step_sec
        self.origin_ts = origin_ts
        self.last_t: float | None = None
        self.last_seen: dict[str, float | None] = {rx: None for rx in RX_IDS}
        self.recent: dict[str, deque] = {rx: deque() for rx in RX_IDS}
        self.heat: dict[int, _HeatBin] = {}
        self.frames = 0

    @property
    def threshold(self) -> float:
        return self.monitor.start_thr

    def push(self, ts: float, rx: str, amplitude: np.ndarray) -> None:
        if self.origin_ts is None:
            self.origin_ts = ts
        self.push_relative(ts - self.origin_ts, rx, amplitude)

    def push_relative(self, t: float, rx: str, amplitude: np.ndarray) -> None:
        if self.origin_ts is None:
            self.origin_ts = 0.0
        rx = rx.strip().upper()
        amplitude = np.asarray(amplitude, dtype=np.float64)
        self.monitor.push_packet(t, rx, amplitude)
        self.last_t = t if self.last_t is None else max(self.last_t, t)
        if rx not in self.last_seen:
            return
        self.last_seen[rx] = t
        self.recent[rx].append(t)
        self._add_heat(t, amplitude)

    def poll(self, ts: float | None = None) -> list[Frame]:
        if self.last_t is None:
            return []
        now = self.last_t if ts is None else ts - self.origin_ts
        return self.poll_relative(now)

    def poll_relative(self, now: float) -> list[Frame]:
        return [self._frame(o) for o in self.monitor.poll(now)]

    def flush(self) -> list[Frame]:
        if self.last_t is None:
            return []
        return self.poll_relative(self.last_t + 2.0)

    def rx_status(self, ts: float | None = None) -> list[dict]:
        if self.last_t is None:
            now = None
        else:
            now = self.last_t if ts is None else ts - self.origin_ts
        out = []
        for rx in RX_IDS:
            seen = self.last_seen[rx]
            rate = self._rate(rx, now)
            if seen is None or now is None:
                code = "none"
            elif now - seen > self.rx_policy.lost_sec:
                code = "lost"
            elif now >= self.rx_policy.rate_window_sec and rate < self.rx_policy.weak_rate:
                code = "weak"
            else:
                code = "ok"
            out.append({
                "rx": rx,
                "status": code,
                "status_ko": RX_STATUS_KO[code],
                "packet_rate": rate,
                "last_packet_ts": None if seen is None else self.to_ts(seen),
            })
        return out

    def events(self) -> list[dict]:
        return [self._event(e) for e in self.monitor.events]

    def open_events(self) -> list[dict]:
        return [self._event(e) for e in self.monitor.open_events()]

    def close_event(self, event_id: int, result: str) -> dict:
        if result not in GUARDIAN_RESULTS:
            raise ValueError(f"result must be one of {GUARDIAN_RESULTS}")
        for e in self.monitor.events:
            if e["id"] == event_id:
                self.monitor.guardian_close(event_id, result)
                return self._event(e)
        raise KeyError(event_id)

    def summary(self) -> dict:
        return self.monitor.summary()

    def to_ts(self, t: float) -> float:
        return round((self.origin_ts or 0.0) + t, 3)

    def _decision_origin(self) -> float:
        det_t0 = self.monitor.det.t0
        return det_t0 if det_t0 is not None else 0.0

    def _frame(self, o: MonitorOutput) -> Frame:
        rel = self._decision_origin() + o.t
        self.frames += 1
        return Frame(
            ts=self.to_ts(rel),
            t=round(rel, 3),
            state=o.state,
            state_ko=o.state_ko,
            motion_index=o.motion_index,
            activity_score=o.activity_score,
            candidate_sec=o.candidate_sec,
            event_id=None if o.event is None else o.event["id"],
            alert=o.alert,
            signal_ok=bool(o.signal["ok"]),
            packet_rate=dict(o.signal["packet_rate"]),
            heatmap=self._take_heat(rel),
        )

    def _event(self, e: dict) -> dict:
        base = self._decision_origin()
        end = e["detection_end"]
        return {
            "id": e["id"],
            "start_ts": self.to_ts(base + e["start"]),
            "alert_ts": self.to_ts(base + e["alert_t"]),
            "end_ts": None if end is None else self.to_ts(base + end),
            "duration_sec": e["duration"],
            "ongoing": end is None,
            "guardian_result": e["guardian"],
        }

    def _rate(self, rx: str, now: float | None) -> float:
        q = self.recent[rx]
        if now is None:
            return 0.0
        while q and q[0] < now - self.rx_policy.rate_window_sec:
            q.popleft()
        return round(len(q) / self.rx_policy.rate_window_sec, 1)

    def _bin(self, t: float) -> int:
        return int(np.floor(t / self.step_sec + 0.5))

    def _add_heat(self, t: float, amplitude: np.ndarray) -> None:
        ok = np.isfinite(amplitude)
        if amplitude.shape != (N_SUB,) or not ok.any():
            return
        b = self.heat.setdefault(self._bin(t), _HeatBin())
        b.total[ok] += amplitude[ok]
        b.count[ok] += 1

    def _take_heat(self, t: float) -> list[float] | None:
        key = self._bin(t)
        for k in [k for k in self.heat if k < key]:
            del self.heat[k]
        b = self.heat.pop(key, None)
        if b is None or not b.count.any():
            return None
        with np.errstate(invalid="ignore", divide="ignore"):
            row = b.total / b.count
        return [None if not np.isfinite(v) else round(float(v), 2) for v in row]


def build_engine(settings, origin_ts: float | None = None, baseline: float | None = None) -> MotionEngine:
    policy = RxPolicy(settings.rx_weak_rate, settings.rx_rate_window_sec, settings.rx_lost_sec)
    return MotionEngine(settings.model_path, origin_ts=origin_ts, rx_policy=policy, baseline=baseline,
                        step_sec=settings.frame_step_sec)


def run_table(engine: MotionEngine, table, on_frames=None) -> list[Frame]:
    frames: list[Frame] = []
    for t, rx, amplitude in table.packets():
        engine.push_relative(t, rx, amplitude)
        batch = engine.poll_relative(t)
        if batch:
            frames.extend(batch)
            if on_frames is not None:
                on_frames(batch, t)
    tail = engine.flush()
    frames.extend(tail)
    if tail and on_frames is not None:
        on_frames(tail, engine.last_t)
    return frames
