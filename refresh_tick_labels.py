"""Re-apply the current label files to outputs/cache/ticks.csv (after relabelling).

The motion indices do not depend on labels, so the slow CSI replay is not needed.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from motion_state.features import frame_labels, load_labels

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--ticks", type=Path, default=Path("outputs/cache/ticks.csv"))
    args = ap.parse_args()
    d = pd.read_csv(args.ticks)
    changed = 0
    for sid, idx in d.groupby("sample_id").groups.items():
        lab = load_labels(next(args.data.glob(f"labels/**/{sid}_labels.csv")))
        t = d.loc[idx, "t"].to_numpy()
        new_toss, new_walk = frame_labels(t - 0.5, lab), frame_labels(t - 1.0, lab)
        changed += int((d.loc[idx, "label_walk"].to_numpy() != new_walk).sum())
        d.loc[idx, "label_toss"], d.loc[idx, "label_walk"] = new_toss, new_walk
    d.to_csv(args.ticks, index=False)
    print(f"refreshed labels; {changed} ticks changed label")
