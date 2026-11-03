from __future__ import annotations

import math
from collections import deque

import numpy as np

from app.pose.packets import SIGNAL_LEN, SIGNAL_START, LinkRecord

VALID_INDEX = np.r_[1:27, 38:64]
N_SUB = len(VALID_INDEX)


def lltf_amplitude(entry: bytes) -> np.ndarray:
    pairs = np.frombuffer(entry[SIGNAL_START:SIGNAL_START + SIGNAL_LEN], dtype=np.int8).astype(np.float32).reshape(128, 2)
    return np.hypot(pairs[:64, 0], pairs[:64, 1])[VALID_INDEX]


class SignalTracker:
    def __init__(self, links: list[str], step_sec: float = 0.5, hz: float = 10.0, lag_frames: int = 3,
                 window_frames: int = 10, max_age: float = 0.3, fresh_sec: float = 1.0,
                 calibration_sec: float = 20.0, activity_age: float = 1.0, observing: float = 0.2, delay_sec: float = 0.2):
        self.links = list(links)
        self.index = {k: i for i, k in enumerate(self.links)}
        self.step_sec, self.dt = step_sec, 1.0 / hz
        self.lag, self.window = lag_frames, window_frames
        self.max_age, self.fresh_sec = max_age, fresh_sec
        self.calibration_frames = int(round(calibration_sec / step_sec))
        self.activity_age, self.observing, self.delay = activity_age, observing, delay_sec
        self.reset()

    def reset(self) -> None:
        n = len(self.links)
        self.shape = np.zeros((n, N_SUB), np.float32)
        self.last_seen = np.full(n, -np.inf)
        self.heat_total = np.zeros(N_SUB, np.float64)
        self.heat_count = 0
        self.history: deque = deque(maxlen=self.window + self.lag + 1)
        self.next_frame: float | None = None
        self.next_out: float | None = None
        self.calib: list[float] = []
        self.baseline: float | None = None
        self.activity: deque = deque(maxlen=200)

    def add(self, ts: float, record: LinkRecord) -> None:
        i = self.index.get(record.link)
        if i is None:
            return
        amp = lltf_amplitude(record.entry)
        norm = float(np.linalg.norm(amp))
        if norm > 0:
            self.shape[i] = amp / norm
            self.last_seen[i] = ts
        self.heat_total += amp
        self.heat_count += 1
        if self.next_frame is None:
            self.next_frame = math.floor(ts / self.dt + 1) * self.dt
            self.next_out = math.floor(ts / self.step_sec + 1) * self.step_sec

    def set_activity(self, ts: float, score: float | None) -> None:
        if score is not None:
            self.activity.append((ts, float(score)))

    def poll(self, upto: float, max_lag: float = 5.0) -> list[dict]:
        if self.next_frame is None:
            return []
        upto -= self.delay
        if upto - self.next_frame > max_lag:
            self.next_frame = math.floor(upto / self.dt) * self.dt
            self.next_out = math.floor(upto / self.step_sec) * self.step_sec
            self.history.clear()
        out = []
        while self.next_frame <= upto:
            t = self.next_frame
            self.next_frame = round(t + self.dt, 6)
            fresh = (t - self.last_seen) <= self.max_age
            self.history.append((self.shape.copy(), fresh))
            if t + 1e-9 >= self.next_out:
                out.append(self._frame(self.next_out, t))
                self.next_out = round(self.next_out + self.step_sec, 6)
        return out

    def _motion(self) -> float | None:
        frames = list(self.history)
        if len(frames) < self.window + self.lag:
            return None
        dists = []
        for k in range(len(frames) - self.window, len(frames)):
            a, fa = frames[k]
            b, fb = frames[k - self.lag]
            ok = fa & fb
            if ok.any():
                dists.append(np.where(ok, np.linalg.norm(a - b, axis=1), np.nan))
        if not dists:
            return None
        per_link = np.nanmean(np.stack(dists), axis=0)
        if np.all(np.isnan(per_link)):
            return None
        return float(np.log(np.nanmedian(per_link) + 1e-6))

    def _frame(self, ts: float, now: float) -> dict:
        signal_ok = bool(np.all(now - self.last_seen <= self.fresh_sec))
        raw = self._motion() if signal_ok else None
        if raw is not None and self.baseline is None:
            self.calib.append(raw)
            if len(self.calib) >= self.calibration_frames:
                self.baseline = float(np.median(self.calib))
        motion = None if raw is None or self.baseline is None else round(raw - self.baseline, 4)
        activity = None
        for at, score in reversed(self.activity):
            if at <= ts + 1e-6:
                if ts - at <= self.activity_age:
                    activity = round(score, 4)
                break
        if not signal_ok:
            state = "signal_check"
        elif self.baseline is None:
            state = "baseline_prep"
        elif activity is not None and activity >= self.observing:
            state = "observing"
        else:
            state = "low_motion"
        heat = None
        if self.heat_count:
            heat = [round(float(v), 2) for v in self.heat_total / self.heat_count]
        self.heat_total[:] = 0
        self.heat_count = 0
        return {"ts": round(ts, 3), "state": state, "motion_index": motion, "activity_score": activity,
                "event_id": None, "signal_ok": signal_ok, "heatmap": heat}
