"""v2 features for lying (static) vs dynamic.

Each decision time t (every HOP_SEC) uses frames in [t - W + L, t + L] only, where L is the
look-ahead (decision latency). With L = 0 the features are causal. The same function runs on
a whole recording (training) or on a rolling frame buffer (streaming).

Feature families (per rx-stream, then aggregated over the 6 streams):
  motion   : mean spectrum distance at lags 0.1/0.3/0.5/1 s over 1/2/4 s windows
  spread   : spectrum standard deviation over the window
  level    : amplitude coefficient of variation
  burst    : strongest 0.5 s block inside the 2 s window
  shift    : distance between the last 1 s mean spectrum and the mean 3-5 s earlier
             (a lasting posture change, e.g. getting up, versus tossing in place)
Relative versions (rel_*) subtract the recording's own running 10th percentile over the past
60 s, so no statistics from other recordings are involved.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .csi_io import FPS

HOP_SEC = 0.5
LAGS = (1, 3, 5, 10)
WINDOWS = (10, 20, 40)
SHIFT_RECENT, SHIFT_PAST = (0, 10), (30, 50)     # frames before the window end
REL_FLOOR_SEC, REL_QUANTILE, REL_MIN_WINDOWS = 60.0, 0.10, 4
MAX_HISTORY = max(max(WINDOWS) + max(LAGS), SHIFT_PAST[1]) + 1


def _lagged(shape: np.ndarray, lag: int) -> np.ndarray:
    d = np.linalg.norm(shape[lag:] - shape[:-lag], axis=2)
    return np.concatenate([np.repeat(d[:1], lag, axis=0), d])


def _agg(prefix: str, per_stream: np.ndarray, out: dict) -> None:
    v = np.log(per_stream + 1e-6)
    out[f"{prefix}_med"] = float(np.median(v))
    out[f"{prefix}_max"] = float(v.max())
    out[f"{prefix}_mean"] = float(v.mean())


def window_features(shape: np.ndarray, level: np.ndarray, lagged: dict[int, np.ndarray], end: int) -> dict:
    """Features of frames before `end` (exclusive). Windows are truncated at the recording start."""
    out: dict[str, float] = {}
    for w in WINDOWS:
        lo = max(0, end - w)
        for lag in LAGS:
            _agg(f"d{lag}_w{w}", lagged[lag][lo:end].mean(0), out)
        _agg(f"spread_w{w}", shape[lo:end].std(0).mean(1) + 1e-9, out)
        lv = level[lo:end]
        _agg(f"lvlcv_w{w}", lv.std(0) / (lv.mean(0) + 1e-9) + 1e-9, out)
    lo = max(0, end - 20)
    blocks = lagged[3][lo:end]
    n = len(blocks) // 5 * 5
    if n:
        _agg("burst_w20", blocks[-n:].reshape(-1, 5, blocks.shape[1]).mean(1).max(0), out)
    else:
        _agg("burst_w20", blocks.mean(0), out)
    r0, r1 = max(0, end - SHIFT_RECENT[1] - 10), max(1, end - SHIFT_RECENT[0])
    p0, p1 = max(0, end - SHIFT_PAST[1]), max(1, end - SHIFT_PAST[0])
    shift = np.linalg.norm(shape[r0:r1].mean(0) - shape[p0:p1].mean(0), axis=1)
    _agg("shift_3to5s", shift + 1e-9, out)
    return out


def recording_features(shape: np.ndarray, level: np.ndarray, time_sec: np.ndarray,
                       lookahead_sec: float) -> pd.DataFrame:
    """Absolute features at every HOP_SEC decision time of one recording."""
    lagged = {lag: _lagged(shape, lag) for lag in LAGS}
    hop, look = int(HOP_SEC * FPS), int(round(lookahead_sec * FPS))
    rows = []
    for c in range(0, len(time_sec), hop):          # c = decision frame
        end = c + look + 1
        if end > len(time_sec) or end < 10:          # need the look-ahead and at least 1 s of data
            continue
        row = {"t": float(time_sec[c])}
        row.update(window_features(shape, level, lagged, end))
        rows.append(row)
    return pd.DataFrame(rows)


def add_relative(df: pd.DataFrame, cols: list[str], group: str = "sample_id") -> pd.DataFrame:
    """rel_f = f - running 10th percentile of f over the past 60 s of the same recording."""
    n = int(REL_FLOOR_SEC / HOP_SEC)
    rel = {}
    for c in cols:
        floor = df.groupby(group)[c].transform(
            lambda x: x.rolling(n, min_periods=1).quantile(REL_QUANTILE))
        count = df.groupby(group).cumcount() + 1
        rel[f"rel_{c}"] = np.where(count >= REL_MIN_WINDOWS, df[c] - floor, 0.0)
    return pd.concat([df, pd.DataFrame(rel, index=df.index)], axis=1)
