"""Replay every recording through the real-time monitor and cache raw per-tick indices.

Baseline is fixed at 0 so motion_index / walk_index are the raw log indices; thresholds and
baselines are then fitted offline on this table (fit_realtime.py) with the same code path
the live monitor uses.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import pandas as pd

from motion_state.features import frame_labels, load_labels
from motion_state.realtime import MonitorConfig
from motion_state.replay import replay

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, default=Path("outputs/cache/ticks.csv"))
    args = ap.parse_args()
    cfg = replace(MonitorConfig(), baseline_update=False)
    rows = []
    raws = sorted(args.data.glob("raw_csi/**/*_csi_raw.csv"))
    for i, path in enumerate(raws, 1):
        sid = path.name.removesuffix("_csi_raw.csv")
        labels = list(args.data.glob(f"labels/**/{sid}_labels.csv"))
        if not labels:
            continue
        date = open(path, encoding="utf-8-sig").readlines()[1][:8]
        outs, _ = replay(path, cfg, baseline={"toss": 0.0, "walk": 0.0})
        lab = load_labels(labels[0])
        t = pd.Series([o.t for o in outs])
        # label at the centre of each index window (1 s window -> t-0.5, 2 s window -> t-1)
        lab_toss = frame_labels((t - 0.5).to_numpy(), lab)
        lab_walk = frame_labels((t - 1.0).to_numpy(), lab)
        for o, lt, lw in zip(outs, lab_toss, lab_walk):
            rows.append({"sample_id": sid, "date": date, "t": o.t, "signal_ok": o.signal["ok"],
                         "toss_raw": o.motion_index, "walk_raw": o.walk_index,
                         "label_toss": lt, "label_walk": lw})
        print(f"[{i}/{len(raws)}] {sid} ticks={len(outs)}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(args.out, index=False)
