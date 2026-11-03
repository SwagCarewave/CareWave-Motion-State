from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.pose.features import FeatureBuilder
from app.pose.registry import load_runtime
from app.pose.packets import iter_records


def _clock(value: str) -> float:
    parts = [float(p) for p in str(value).strip().split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def clock_mapping(labels: Path) -> tuple[float, float]:
    d = pd.read_csv(labels)
    if {"video_start", "video_end"} <= set(d):
        d["video_start_s"] = d["video_start"].map(_clock)
        d["video_end_s"] = d["video_end"].map(_clock)
    vs = np.r_[d.video_start_s, d.video_end_s].astype(float)
    pc = np.r_[d.pc_start, d.pc_end].astype(float)
    pivot = float(vs.mean())
    a, b0 = np.polyfit(vs - pivot, pc - pc.mean(), 1)
    return float(a), float(pc.mean() + b0 - a * pivot)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="원본 raw.bin을 실시간 경로로 돌려 저장된 예측과 비교합니다.")
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--expected", type=Path, required=True, help="노트북이 저장한 *_predictions.npz")
    ap.add_argument("--model", type=Path, required=True, help="models/pose/<이름> 폴더")
    args = ap.parse_args()

    runtime = load_runtime(args.model)
    if not hasattr(runtime, "model"):
        sys.exit("이 스크립트는 upper3_gru(학습 노트북 예측 비교) 모델용입니다. 13관절 모델은 verify_tcn.py를 쓰세요.")
    model = runtime.model
    expected = np.load(args.expected)
    ends = expected["time"]
    a, b = clock_mapping(args.labels)
    step = 1.0 / model.hz
    first = ends[0] - step * model.window
    grid = first + step * np.arange(int(round((ends[-1] - first) / step)) + 1)
    targets = a * grid + b

    builder = FeatureBuilder(model.links, model.max_age)
    records = iter_records(args.raw)
    pending = next(records, None)
    frames: list[np.ndarray | None] = []
    for target in targets:
        while pending is not None and pending[0] <= target:
            builder.add(pending[0], pending[1].link, pending[1].amplitude)
            pending = next(records, None)
        frames.append(builder.frame(target)[0])

    index = {round(g, 1): i for i, g in enumerate(grid)}
    windows, missing = [], 0
    for te in ends:
        e = index[round(te, 1)]
        chunk = frames[e - model.window + 1:e + 1]
        if any(f is None for f in chunk):
            missing += 1
            windows.append(np.zeros((model.window, model.n_features), np.float32))
        else:
            windows.append(np.stack(chunk))
    windows = np.stack(windows)
    xy, prob = [], []
    for s in range(0, len(windows), 64):
        p, q = model.predict(windows[s:s + 64])
        xy.append(p)
        prob.append(q)
    xy, prob = np.concatenate(xy), np.concatenate(prob)
    ok = np.ones(len(ends), bool)
    ok[[i for i, w in enumerate(windows) if not w.any()]] = False
    dxy = np.abs(xy - expected["prediction"])[ok]
    dp = np.abs(prob - expected["probability"])[ok]
    same = (prob.argmax(1) == expected["probability"].argmax(1))[ok]
    print(f"clock a={a:.6f} b={b:.3f}")
    print(f"windows {len(ends)}, rebuilt {ok.sum()}, missing {missing}")
    print(f"xy abs diff: max {dxy.max():.2e}, mean {dxy.mean():.2e}")
    print(f"prob abs diff: max {dp.max():.2e}, mean {dp.mean():.2e}")
    print(f"action argmax agreement: {same.mean() * 100:.2f}%")


if __name__ == "__main__":
    main()
