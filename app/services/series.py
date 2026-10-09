from __future__ import annotations

import math
from typing import Iterable

import numpy as np

POINT_FIELDS = ("ts", "state", "motion_index", "activity_score", "event_id", "signal_ok", "heatmap")
BURST_LABEL = "움직임 급증"
SHORT_LABEL = "짧은 움직임"


def event_label(duration_sec: float | None, burst_sec: float) -> str:
    return BURST_LABEL if (duration_sec or 0.0) >= burst_sec else SHORT_LABEL


def _mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(float(np.mean(vals)), 4) if vals else None


def downsample(frames: list[dict], max_points: int) -> tuple[list[dict], int]:
    if not frames or len(frames) <= max_points:
        return frames, 1
    size = math.ceil(len(frames) / max_points)
    out = []
    for i in range(0, len(frames), size):
        chunk = frames[i:i + size]
        rows = [c["heatmap"] for c in chunk if c.get("heatmap")]
        events = [c["event_id"] for c in chunk if c.get("event_id") is not None]
        scores = [c["activity_score"] for c in chunk if c.get("activity_score") is not None]
        out.append({
            "ts": chunk[0]["ts"],
            "state": chunk[-1]["state"],
            "motion_index": _mean(c.get("motion_index") for c in chunk),
            "activity_score": round(max(scores), 4) if scores else None,
            "event_id": events[-1] if events else None,
            "signal_ok": all(c.get("signal_ok", True) for c in chunk),
            "heatmap": [round(float(v), 2) for v in np.mean(np.array(rows, dtype=float), axis=0)] if rows else None,
        })
    return out, size


def build_series(frames: list[dict], events: list[dict], start: float, end: float, max_points: int,
                 heatmap: bool, threshold: float | None, step_sec: float, burst_sec: float) -> dict:
    picked = [f for f in frames if start <= f["ts"] <= end]
    points, factor = downsample(picked, max_points)
    return {
        "from_ts": round(start, 3),
        "to_ts": round(end, 3),
        "step_sec": step_sec * factor,
        "activity_threshold": None if threshold is None else round(threshold, 4),
        "count": len(points),
        "ts": [p["ts"] for p in points],
        "motion_index": [p["motion_index"] for p in points],
        "activity_score": [p["activity_score"] for p in points],
        "state": [p["state"] for p in points],
        "event_id": [p["event_id"] for p in points],
        "signal_ok": [p["signal_ok"] for p in points],
        "heatmap": [p["heatmap"] for p in points] if heatmap else None,
        "events": [{**e, "label": event_label(e["duration_sec"], burst_sec)} for e in events
                   if e["start_ts"] <= end and (e["end_ts"] or end) >= start],
    }


def columns_to_points(columns: dict) -> list[dict]:
    return [dict(zip(POINT_FIELDS, row)) for row in zip(*(columns[k] for k in POINT_FIELDS))]


def points_to_columns(points: list[dict]) -> dict:
    return {k: [p[k] for p in points] for k in POINT_FIELDS}
