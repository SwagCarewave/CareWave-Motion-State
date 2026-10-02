"""Streaming lying/dynamic detector (v2 model).

Feed raw packets with push_packet(t, rx, amplitude); poll(now) returns one decision every
0.5 s. Each decision is for time (now - lookahead): the model looks 1 s past the decision
time, so results arrive about 1 s after the moment they describe.
"""
from __future__ import annotations

import pickle
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .csi_io import FPS, N_SUB, RX_IDS
from .features_v2 import HOP_SEC, LAGS, MAX_HISTORY, REL_FLOOR_SEC, REL_MIN_WINDOWS, REL_QUANTILE, window_features
from .realtime import _StreamSplitter

N_STREAMS = len(RX_IDS) * 2


@dataclass
class Decision:
    t: float              # moment the decision describes (seconds since first packet)
    state: str            # "lying" (static) | "dynamic" | "warming_up"
    probability: float | None
    motion: float | None = None   # log mean 0.3 s-lag spectrum change over 1 s (graph value)


class LyingDynamicDetector:
    def __init__(self, model_path: Path = Path("models/lying_dynamic_v2.pkl"), init_packets: int = 60):
        bundle = pickle.loads(Path(model_path).read_bytes())
        self.model, self.columns = bundle["model"], bundle["columns"]
        self.threshold, self.smooth = bundle["threshold"], bundle["smooth"]
        self.look = int(round(bundle["lookahead_sec"] * FPS))
        # rel_f = f - running 10th percentile of f over the past 60 s (same as features_v2.add_relative)
        self.rel_bases = [c[4:] for c in self.columns if c.startswith("rel_")]
        self.rel_hist: deque = deque(maxlen=int(REL_FLOOR_SEC / HOP_SEC))
        self.splitters = {rx: _StreamSplitter(init_packets) for rx in RX_IDS}
        self.t0: float | None = None
        self.bins: dict[int, dict[int, list]] = {}
        self.last_shape = np.full((N_STREAMS, N_SUB), np.nan)
        self.last_level = np.full(N_STREAMS, np.nan)
        keep = MAX_HISTORY + self.look + max(LAGS) + 2
        self.shape_buf: deque = deque(maxlen=keep)
        self.level_buf: deque = deque(maxlen=keep)
        self.n_frames = 0
        self.next_decision = 0              # decision frame index (every HOP)
        self.recent_p: deque = deque(maxlen=self.smooth)

    def push_packet(self, t: float, rx: str, amplitude: np.ndarray) -> None:
        if rx not in self.splitters or not np.all(np.isfinite(amplitude)):
            return
        norm = float(np.linalg.norm(amplitude))
        if norm <= 0:
            return
        if self.t0 is None:
            self.t0 = t
        for tt, k, shape in self.splitters[rx].push(t - self.t0, amplitude / norm):
            b = int(np.rint(tt * FPS))
            self.bins.setdefault(b, {}).setdefault(RX_IDS.index(rx) * 2 + k, []).append((shape, norm))

    def poll(self, now: float) -> list[Decision]:
        if self.t0 is None:
            return []
        last_final = int(np.floor((now - self.t0) * FPS)) - 1
        out = []
        while self.n_frames <= last_final:
            got = self.bins.pop(self.n_frames, {})
            shape, level = self.last_shape.copy(), self.last_level.copy()
            for s, items in got.items():
                m = np.mean([x[0] for x in items], axis=0)
                shape[s] = m / np.linalg.norm(m)
                level[s] = np.mean([x[1] for x in items])
            self.last_shape, self.last_level = shape, level
            self.shape_buf.append(shape)
            self.level_buf.append(level)
            self.n_frames += 1
            out.extend(self._decide_ready())
        return out

    def _decide_ready(self) -> list[Decision]:
        hop = int(HOP_SEC * FPS)
        out = []
        while self.next_decision + self.look < self.n_frames:
            c = self.next_decision
            self.next_decision += hop
            end_abs = c + self.look + 1                     # exclusive, in absolute frame index
            if end_abs < 10:
                continue
            first_abs = self.n_frames - len(self.shape_buf)
            shape = np.stack(self.shape_buf)[: end_abs - first_abs]
            level = np.stack(self.level_buf)[: end_abs - first_abs]
            if np.isnan(shape[-min(len(shape), MAX_HISTORY):]).any():
                out.append(Decision(c / FPS, "warming_up", None))
                continue
            lagged = {lag: _lagged(shape, lag) for lag in LAGS}
            feats = window_features(shape, level, lagged, len(shape))
            if self.rel_bases:
                self.rel_hist.append([feats[b] for b in self.rel_bases])
                if len(self.rel_hist) >= REL_MIN_WINDOWS:
                    floor = np.quantile(np.array(self.rel_hist), REL_QUANTILE, axis=0)
                    feats.update({f"rel_{b}": feats[b] - f for b, f in zip(self.rel_bases, floor)})
                else:
                    feats.update({f"rel_{b}": 0.0 for b in self.rel_bases})
            x = pd.DataFrame([[feats[k] for k in self.columns]], columns=self.columns)
            p = float(self.model.predict_proba(x)[:, 1][0])
            self.recent_p.append(p)
            ps = float(np.mean(self.recent_p))
            out.append(Decision(c / FPS, "dynamic" if ps >= self.threshold else "lying", round(ps, 4),
                                round(feats["d3_w10_med"], 4)))
        return out


def _lagged(shape: np.ndarray, lag: int) -> np.ndarray:
    if len(shape) <= lag:
        return np.zeros((len(shape), shape.shape[1]))
    d = np.linalg.norm(shape[lag:] - shape[:-lag], axis=2)
    return np.concatenate([np.repeat(d[:1], lag, axis=0), d])
