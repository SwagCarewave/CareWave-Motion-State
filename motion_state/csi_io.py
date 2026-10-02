"""Raw CSI loading: packet-stream split and 10 Hz resampling.

Each RX receives two interleaved packet types whose amplitude spectra are strongly
anti-correlated. They are split causally (see assign_packet_streams) and resampled to a
common 10 Hz grid. Logic ported from CareWave-Action-Model/csi_dataset.py.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

RX_IDS = ("RX1", "RX2", "RX3")
N_SUB = 52
SUB_COLS = [f"sub_{i}" for i in range(N_SUB)]
FPS = 10


@dataclass
class CsiStream:
    sample_id: str
    date: str
    time_sec: np.ndarray   # (T,) frame times from first packet
    shape: np.ndarray      # (T, 6, 52) unit-norm spectrum per rx-stream
    level: np.ndarray      # (T, 6) amplitude norm per rx-stream
    change: np.ndarray     # (T, 6) packet-to-packet shape change per rx-stream
    separation: list       # per-RX stream separation quality


def read_raw_csi(path: Path) -> pd.DataFrame:
    """Read raw exports, right-padding legacy short rows with NaN."""
    try:
        raw = pd.read_csv(path)
        if set(SUB_COLS).issubset(raw.columns):
            return raw
    except pd.errors.ParserError:
        pass
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        columns = header[:4] + SUB_COLS
        rows = [row + [None] * (len(columns) - len(row)) for row in reader if row]
    return pd.DataFrame(rows, columns=columns)


def assign_packet_streams(shapes: np.ndarray, init_packets: int = 60, ema_alpha: float = 0.05) -> np.ndarray:
    """Causally split one receiver's packets into its two interleaved spectral streams.

    Centroids start from 2-means on the first packets, then follow slow drift with an EMA;
    each packet only uses past data, so the same code works for streaming inference.
    """
    from sklearn.cluster import KMeans

    init = shapes[:max(4, min(init_packets, len(shapes)))]
    centroids = KMeans(2, n_init=5, random_state=0).fit(init).cluster_centers_
    centroids /= np.linalg.norm(centroids, axis=1, keepdims=True)
    # Canonical order so stream 0/1 means the same thing across recordings.
    half = shapes.shape[1] // 2
    if (centroids[0, :half] ** 2).sum() < (centroids[1, :half] ** 2).sum():
        centroids = centroids[::-1].copy()
    labels = np.empty(len(shapes), dtype=np.int64)
    for i, shape in enumerate(shapes):
        k = int(np.argmax(centroids @ shape))
        labels[i] = k
        centroids[k] = (1.0 - ema_alpha) * centroids[k] + ema_alpha * shape
        centroids[k] /= np.linalg.norm(centroids[k])
    return labels


def stream_separation(shapes: np.ndarray, labels: np.ndarray) -> float | None:
    """Median of (cosine to own stream mean - cosine to the other stream mean); near 0 = poorly split."""
    if (labels == 0).sum() < 2 or (labels == 1).sum() < 2:
        return None
    means = np.stack([shapes[labels == k].mean(axis=0) for k in (0, 1)])
    means /= np.linalg.norm(means, axis=1, keepdims=True)
    similarity = shapes @ means.T
    idx = np.arange(len(labels))
    return round(float(np.median(similarity[idx, labels] - similarity[idx, 1 - labels])), 4)


def load_csi(path: Path, fill: str = "interpolate") -> CsiStream:
    """fill="interpolate" uses later packets (offline only); fill="ffill" repeats the last
    value, exactly what a live stream can do, so training matches real-time input."""
    raw = read_raw_csi(path)
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True, errors="coerce")
    raw["rx"] = raw["rx"].astype(str).str.strip().str.upper()
    raw[SUB_COLS] = raw[SUB_COLS].apply(pd.to_numeric, errors="coerce")
    raw = raw.dropna(subset=["timestamp"])
    raw = raw[raw["rx"].isin(RX_IDS)].sort_values("timestamp", kind="stable")
    date = str(raw["experiment_id"].iloc[0])[:8]
    origin = raw["timestamp"].min()
    raw["t"] = (raw["timestamp"] - origin).dt.total_seconds()
    full_bins = np.arange(0, int(np.rint(raw["t"].max() * FPS)) + 1)

    shapes_out, levels_out, changes_out, separation = [], [], [], []
    for rx in RX_IDS:
        part = raw[raw["rx"].eq(rx)]
        part = part[part[SUB_COLS].notna().all(axis=1)]
        amplitude = part[SUB_COLS].to_numpy(np.float64)
        norm = np.linalg.norm(amplitude, axis=1)
        keep = norm > 0
        part, amplitude, norm = part[keep], amplitude[keep], norm[keep]
        shapes = amplitude / norm[:, None]
        labels = assign_packet_streams(shapes)
        separation.append({"rx": rx, "separation": stream_separation(shapes, labels)})
        bins = np.rint(part["t"].to_numpy() * FPS).astype(int)
        for stream in (0, 1):
            mask = labels == stream
            s = shapes[mask]
            change = np.full(len(s), np.nan)
            change[1:] = np.linalg.norm(np.diff(s, axis=0), axis=1)
            frame = pd.DataFrame(s, columns=SUB_COLS)
            frame["level"] = norm[mask]
            frame["change"] = change
            frame["bin"] = bins[mask]
            binned = frame.groupby("bin").mean().reindex(full_bins)
            if fill == "ffill":
                binned = binned.ffill().bfill()   # bfill only covers frames before the first packet
            else:
                binned = binned.interpolate(axis=0, limit_direction="both")
            shapes_out.append(binned[SUB_COLS].to_numpy(np.float32))
            levels_out.append(binned["level"].to_numpy(np.float32))
            changes_out.append(binned["change"].to_numpy(np.float32))

    shape = np.stack(shapes_out, axis=1)
    shape /= np.linalg.norm(shape, axis=2, keepdims=True) + 1e-9
    return CsiStream(
        sample_id=path.name.removesuffix("_csi_raw.csv"), date=date,
        time_sec=full_bins / FPS, shape=shape,
        level=np.stack(levels_out, axis=1), change=np.stack(changes_out, axis=1),
        separation=separation,
    )
