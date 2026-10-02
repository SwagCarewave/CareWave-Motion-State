"""Train the final static/dynamic model on every cached recording and save it."""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from motion_state.model import MotionStateModel

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=Path("outputs/cache"))
    ap.add_argument("--out", type=Path, default=Path("models/motion_state_v1.pkl"))
    args = ap.parse_args()
    df = pd.read_csv(args.cache / "windows.csv")
    model = MotionStateModel().fit(df)
    model.save(args.out)
    print(f"saved {args.out} threshold={model.threshold:.4f} features={len(model.cols)} "
          f"recordings={df.sample_id.nunique()} windows={len(df)}")
