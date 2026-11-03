from __future__ import annotations

from collections import deque

import numpy as np

from app.pose.packets import N_BINS


class LinkHistory:
    def __init__(self, keep_sec: float):
        self.keep_sec = keep_sec
        self.items: deque = deque()
        self.pending_ts: float | None = None
        self.pending: list[np.ndarray] = []

    def add(self, ts: float, amp: np.ndarray) -> None:
        if self.pending_ts is not None and ts == self.pending_ts:
            self.pending.append(amp)
            self.items[-1] = (ts, np.median(np.stack(self.pending), axis=0).astype(np.float32))
            return
        if self.items and ts < self.items[-1][0]:
            return
        self.pending_ts, self.pending = ts, [amp]
        self.items.append((ts, amp.astype(np.float32)))
        while self.items and self.items[0][0] < ts - self.keep_sec:
            self.items.popleft()

    def at(self, target: float) -> tuple[float, np.ndarray] | None:
        found = None
        for ts, amp in reversed(self.items):
            if ts <= target:
                found = (ts, amp)
                break
        return found

    @property
    def last_ts(self) -> float | None:
        return self.items[-1][0] if self.items else None


class FeatureBuilder:
    def __init__(self, links: list[str], max_age: float, keep_sec: float = 2.0):
        self.links = list(links)
        self.index = {k: i for i, k in enumerate(self.links)}
        self.max_age = max_age
        self.history = {k: LinkHistory(keep_sec) for k in self.links}
        self.prev_relative: np.ndarray | None = None
        self.prev_ok = False

    @property
    def n_features(self) -> int:
        return 2 * len(self.links) * N_BINS + len(self.links)

    def add(self, ts: float, link: str, amp: np.ndarray) -> bool:
        h = self.history.get(link)
        if h is None:
            return False
        h.add(ts, amp)
        return True

    def frame(self, target: float) -> tuple[np.ndarray | None, bool]:
        raw = np.zeros((len(self.links), N_BINS), np.float32)
        ok = True
        for j, key in enumerate(self.links):
            hit = self.history[key].at(target)
            if hit is None:
                ok = False
                continue
            ts, amp = hit
            raw[j] = amp
            if not 0 <= target - ts <= self.max_age:
                ok = False
        median = np.median(raw, axis=1)
        relative = raw - median[:, None]
        if self.prev_relative is None:
            motion = np.zeros_like(relative)
        else:
            motion = np.abs(relative - self.prev_relative)
        valid = ok and self.prev_ok
        self.prev_relative, self.prev_ok = relative, ok
        if not valid:
            return None, ok
        x = np.concatenate([relative.reshape(-1), motion.reshape(-1), median]).astype(np.float32)
        return x, ok

    def reset(self) -> None:
        self.prev_relative = None
        self.prev_ok = False
