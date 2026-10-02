"""Labels and causal window features for static/dynamic motion classification.

A window ends at time t and uses only past frames, so the same features run in real time.
Its label is taken at the window centre (t - WIN_SEC / 2), giving about 1 s of latency.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .csi_io import FPS, CsiStream

STATIC_LABELS = {"lying", "standing"}
# small movements while lying (user-annotated); kept out of training and of the static/dynamic score
IN_BED_LABELS = {"tossing", "arm_movement", "small_movement"}
IGNORE_LABELS = {"ignore", "unlabeled", "label"}
WIN_SEC = 2.0
HOP_SEC = 0.5
SCALES_SEC = (1.0, 2.0, 4.0)   # trailing windows; truncated at the start of a recording
BOUNDARY_SEC = 1.0             # labels are whole seconds: windows this close to a static/dynamic change are not scored


def load_labels(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    df["label"] = df["label"].astype(str).str.strip().str.lower()
    return df.sort_values(["start_sec", "end_sec"]).reset_index(drop=True)


def frame_labels(times: np.ndarray, intervals: pd.DataFrame) -> np.ndarray:
    result = np.full(len(times), "unlabeled", dtype=object)
    for row in intervals.itertuples(index=False):
        result[(times >= float(row.start_sec)) & (times < float(row.end_sec))] = row.label
    return result


def motion_class(label: str) -> str:
    if label in IGNORE_LABELS:
        return "ignore"
    if label in IN_BED_LABELS:
        return "in_bed"
    return "static" if label in STATIC_LABELS else "dynamic"


def _lag_distance(shape: np.ndarray, lag: int) -> np.ndarray:
    """||s[i] - s[i-lag]|| per frame and stream; first `lag` frames repeat the first value."""
    d = np.linalg.norm(shape[lag:] - shape[:-lag], axis=2)
    return np.concatenate([np.repeat(d[:1], lag, axis=0), d], axis=0)


def stream_signals(csi: CsiStream) -> dict[str, np.ndarray]:
    """Per-frame motion signals, each (T, 6 rx-streams)."""
    return {
        "chg": csi.change,
        "lag3": _lag_distance(csi.shape, 3),
        "lag10": _lag_distance(csi.shape, 10),
        "lvl": csi.level,
    }


def window_features(csi: CsiStream, end: int, sig: dict[str, np.ndarray]) -> dict[str, float]:
    """Features of the trailing windows ending at frame `end` (exclusive)."""
    out: dict[str, float] = {}
    for scale in SCALES_SEC:
        n = int(scale * FPS)
        lo = max(0, end - n)   # truncated at the start of a recording
        tag = f"{int(scale)}s"
        per_stream = {
            "chg": sig["chg"][lo:end].mean(0),
            "lag3": sig["lag3"][lo:end].mean(0),
            "lag10": sig["lag10"][lo:end].mean(0) if scale >= 2 else None,
            # spectrum spread within the window, averaged over subcarriers
            "sstd": csi.shape[lo:end].std(0).mean(1),
            "lvlcv": sig["lvl"][lo:end].std(0) / (sig["lvl"][lo:end].mean(0) + 1e-9),
        }
        for name, v in per_stream.items():
            if v is None:
                continue
            lv = np.log(v + 1e-6)
            out[f"{name}_{tag}_med"] = float(np.median(lv))
            out[f"{name}_{tag}_max"] = float(lv.max())
            out[f"{name}_{tag}_min"] = float(lv.min())
    # peak 0.5 s burst inside the 2 s window, catches short motions such as a head turn
    burst = sig["lag3"][end - 20:end].reshape(4, 5, -1).mean(1)
    out["burst_max"] = float(np.log(burst.max(0) + 1e-6).max())
    out["burst_med"] = float(np.median(np.log(burst.max(0) + 1e-6)))
    return out


def _change_times(times: np.ndarray, classes: np.ndarray) -> np.ndarray:
    """Times where the static/dynamic class changes; ignore frames take the previous class."""
    filled = pd.Series(classes).where(lambda s: s != "ignore").ffill().bfill().to_numpy()
    return times[1:][filled[1:] != filled[:-1]]


def build_windows(csi: CsiStream, intervals: pd.DataFrame) -> pd.DataFrame:
    labels = frame_labels(csi.time_sec, intervals)
    classes = np.array([motion_class(x) for x in labels], dtype=object)
    sig = stream_signals(csi)
    first = int(WIN_SEC * FPS)
    hop = int(HOP_SEC * FPS)
    half = int(WIN_SEC * FPS / 2)
    change_times = _change_times(csi.time_sec, classes)
    rows = []
    for end in range(first, len(csi.time_sec) + 1, hop):
        c = end - half                       # centre frame of the 2 s window
        t = float(csi.time_sec[c])
        dchg = float(np.abs(change_times - t).min()) if len(change_times) else 999.0
        row = {
            "sample_id": csi.sample_id, "date": csi.date, "person": csi.sample_id.split("_")[0],
            "t_end": float(csi.time_sec[end - 1]), "t_center": t,
            "label": labels[c], "cls": classes[c], "dchg": dchg, "boundary": dchg <= BOUNDARY_SEC,
        }
        row.update(window_features(csi, end, sig))
        # mean spectrum over the 2 s window, for the standing-vs-lying analysis
        row["_shape"] = csi.shape[end - 2 * half:end].mean(0).reshape(-1)
        rows.append(row)
    return pd.DataFrame(rows)
