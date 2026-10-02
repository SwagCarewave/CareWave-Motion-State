"""Build the window table from <data>/raw_csi + <data>/labels and cache it under outputs/."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from motion_state.csi_io import load_csi
from motion_state.features import build_windows, load_labels


def build_table(data_dir: Path, verbose: bool = True) -> tuple[pd.DataFrame, np.ndarray, list]:
    tables, shapes, quality = [], [], []
    raws = sorted(data_dir.glob("raw_csi/**/*_csi_raw.csv"))
    for i, raw_path in enumerate(raws, 1):
        sid = raw_path.name.removesuffix("_csi_raw.csv")
        label_paths = list(data_dir.glob(f"labels/**/{sid}_labels.csv"))
        if not label_paths:
            print(f"skip {sid}: no labels")
            continue
        csi = load_csi(raw_path)
        win = build_windows(csi, load_labels(label_paths[0]))
        shapes.append(np.stack(win.pop("_shape").to_list()).astype(np.float32))
        tables.append(win)
        quality.append({"sample_id": sid, "date": csi.date, "frames": len(csi.time_sec), "rx": csi.separation})
        if verbose:
            print(f"[{i}/{len(raws)}] {sid} {csi.date} windows={len(win)}")
    return pd.concat(tables, ignore_index=True), np.concatenate(shapes), quality


def build(data_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    table, shapes, quality = build_table(data_dir)
    table.to_csv(out_dir / "windows.csv", index=False)
    np.save(out_dir / "window_shapes.npy", shapes)
    (out_dir / "quality.json").write_text(json.dumps(quality, indent=1), encoding="utf-8")
    print(table.groupby(["date", "cls"]).size().unstack(fill_value=0))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, default=Path("outputs/cache"))
    args = ap.parse_args()
    build(args.data, args.out)
