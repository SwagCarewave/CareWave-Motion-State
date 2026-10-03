"""Build the v2 window table (features + labels) for every recording and look-ahead.

Static = lying, in-bed movement (tossing / arm_movement / small_movement), standing.
Dynamic = every other labelled action (getting up included). ignore/unlabeled are kept with
cls = "ignore" so they never enter training or scoring.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from motion_state.csi_io import load_csi
from motion_state.features import frame_labels, load_labels
from motion_state.features_v2 import add_relative, recording_features

STATIC = {"lying", "tossing", "arm_movement", "small_movement", "standing"}
IGNORE = {"ignore", "unlabeled", "label"}


IN_BED = {"tossing", "arm_movement", "small_movement"}


def label_class(label: str, tossing: str = "static", standing: str = "static",
                walking_only: bool = False) -> str:
    """tossing / standing: "static", "dynamic" or "exclude" (treated like ignore).
    walking_only: the only dynamic class is walking; other actions are excluded."""
    if label in IGNORE:
        return "ignore"
    if walking_only and label not in STATIC and label not in IN_BED and label != "walking":
        return "ignore"
    if label in IN_BED:
        return "ignore" if tossing == "exclude" else tossing
    if label == "standing":
        return "ignore" if standing == "exclude" else standing
    return "static" if label in STATIC else "dynamic"


def change_distance(t: np.ndarray, cls: np.ndarray) -> np.ndarray:
    filled = pd.Series(cls).where(lambda s: s != "ignore").ffill().bfill().to_numpy()
    ch = t[1:][filled[1:] != filled[:-1]]
    return np.abs(t[:, None] - ch[None, :]).min(1) if len(ch) else np.full(len(t), 999.0)


def main(data: Path, out: Path, lookaheads: list[float], tossing: str = "static", standing: str = "static",
         walking_only: bool = False) -> None:
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "frames_ffill"
    cache.mkdir(exist_ok=True)
    raws = sorted(data.glob("raw_csi/**/*_csi_raw.csv"))
    frames = {}
    for i, p in enumerate(raws, 1):
        sid = p.name.removesuffix("_csi_raw.csv")
        f = cache / f"{sid}.npz"
        if not f.exists():
            c = load_csi(p, fill="ffill")   # same gap filling as the live stream
            np.savez_compressed(f, shape=c.shape, level=c.level, time_sec=c.time_sec, date=c.date)
        z = np.load(f)
        frames[sid] = (z["shape"], z["level"], z["time_sec"], str(z["date"]))
        print(f"[{i}/{len(raws)}] frames {sid}", flush=True)
    for look in lookaheads:
        parts = []
        for sid, (shape, level, t, date) in frames.items():
            lab = load_labels(next(data.glob(f"labels/**/{sid}_labels.csv")))
            df = recording_features(shape, level, t, look)
            labels = frame_labels(df.t.to_numpy(), lab)
            cls = np.array([label_class(x, tossing, standing, walking_only) for x in labels], dtype=object)
            df.insert(0, "sample_id", sid)
            df.insert(1, "date", date)
            df.insert(2, "person", sid.split("_")[0])
            df.insert(3, "label", labels)
            df.insert(4, "cls", cls)
            df.insert(5, "dchg", change_distance(df.t.to_numpy(), cls))
            parts.append(df)
        table = pd.concat(parts, ignore_index=True)
        feats = [c for c in table.columns if c not in ("sample_id", "date", "person", "label", "cls", "dchg", "t")]
        table = add_relative(table, feats)
        tag = "" if (tossing, standing, walking_only) == ("static", "static", False)             else f"_toss-{tossing}_stand-{standing}" + ("_walkonly" if walking_only else "")
        # always CSV: every downstream script reads windows_L*.csv
        table.to_csv(out / f"windows_L{look:g}{tag}.csv", index=False)
        print(f"look-ahead {look}s: {len(table)} windows, {len(feats)} abs features", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, default=Path("outputs/v2"))
    ap.add_argument("--lookahead", type=float, nargs="+", default=[0.0, 1.0])
    ap.add_argument("--tossing", choices=["static", "dynamic", "exclude"], default="static")
    ap.add_argument("--standing", choices=["static", "dynamic", "exclude"], default="static")
    ap.add_argument("--walking-only", action="store_true", help="dynamic = walking only; other actions excluded")
    args = ap.parse_args()
    main(args.data, args.out, args.lookahead, args.tossing, args.standing, args.walking_only)
