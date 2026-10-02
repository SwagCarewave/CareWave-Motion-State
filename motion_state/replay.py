"""Replay a recorded raw CSI file through NightMonitor packet by packet, as if live."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .csi_io import RX_IDS, SUB_COLS, read_raw_csi
from .realtime import MonitorConfig, MonitorOutput, NightMonitor


def iter_packets(path: Path):
    raw = read_raw_csi(path)
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True, errors="coerce")
    raw["rx"] = raw["rx"].astype(str).str.strip().str.upper()
    raw[SUB_COLS] = raw[SUB_COLS].apply(pd.to_numeric, errors="coerce")
    raw = raw.dropna(subset=["timestamp"])
    raw = raw[raw["rx"].isin(RX_IDS)].sort_values("timestamp", kind="stable")
    t = (raw["timestamp"] - raw["timestamp"].iloc[0]).dt.total_seconds().to_numpy()
    amp = raw[SUB_COLS].to_numpy(np.float64)
    for ti, rx, a in zip(t, raw["rx"].to_numpy(), amp):
        yield float(ti), rx, a


def replay(path: Path, config: MonitorConfig | None = None, baseline: dict | None = None
           ) -> tuple[list[MonitorOutput], NightMonitor]:
    mon = NightMonitor(config, baseline)
    outs: list[MonitorOutput] = []
    last_t = 0.0
    for t, rx, a in iter_packets(path):
        mon.push_packet(t, rx, a)
        outs.extend(mon.poll(t))
        last_t = t
    outs.extend(mon.poll(last_t + 0.2))
    return outs, mon
