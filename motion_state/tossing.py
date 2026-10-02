"""In-bed activity: separate tossing from still lying within one night's recording.

Offline (per night) on purpose: the score is normalised by the night's own median, which
removes person/room differences in the still-lying noise level. Steps:
  1. 1 s mean of the lag-0.5 s spectrum distance per rx-stream, mean over streams (log)
  2. minus the night's median
  3. centred 3-window (1.5 s) smoothing
  4. score >= TOSS_THRESHOLD, kept only in runs of >= MIN_EPISODE_SEC
Fitted on the two 0624 lie_down recordings (sujin, hoyeon): leave-one-recording-out balanced
accuracy 0.83, 19/22 tossing episodes caught, about 1.3 false episodes per minute of still lying.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .csi_io import FPS, CsiStream

LAG_FRAMES = 5           # 0.5 s
WIN_FRAMES = 10          # 1 s
HOP_FRAMES = 5           # 0.5 s
SMOOTH_WINDOWS = 3
TOSS_THRESHOLD = 0.057   # log units above the night's median
MIN_EPISODE_SEC = 2.0


def tossing_score(csi: CsiStream) -> pd.DataFrame:
    d = np.linalg.norm(csi.shape[LAG_FRAMES:] - csi.shape[:-LAG_FRAMES], axis=2)
    d = np.concatenate([np.repeat(d[:1], LAG_FRAMES, axis=0), d])
    ends = np.arange(WIN_FRAMES, len(d) + 1, HOP_FRAMES)
    raw = np.log(np.array([d[e - WIN_FRAMES:e].mean() for e in ends]))
    rel = pd.Series(raw - np.median(raw))
    score = rel.rolling(SMOOTH_WINDOWS, min_periods=1, center=True).mean().to_numpy()
    return pd.DataFrame({"t_center": csi.time_sec[ends - WIN_FRAMES // 2], "score": score})


def tossing_episodes(scores: pd.DataFrame, threshold: float = TOSS_THRESHOLD,
                     min_sec: float = MIN_EPISODE_SEC) -> list[tuple[float, float]]:
    """(start, end) seconds of runs with score >= threshold lasting at least min_sec."""
    hop = HOP_FRAMES / FPS
    flagged = scores.score.to_numpy() >= threshold
    t = scores.t_center.to_numpy()
    episodes, start = [], None
    for i, f in enumerate(flagged):
        if f and start is None:
            start = i
        if start is not None and (not f or i == len(flagged) - 1):
            stop = i if f else i - 1
            if (stop - start + 1) * hop >= min_sec:
                episodes.append((float(t[start] - hop / 2), float(t[stop] + hop / 2)))
            start = None
    return episodes
