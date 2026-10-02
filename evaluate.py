"""Evaluate a saved model on any folder laid out like data/ (raw_csi/**, labels/**).

Intended for the private holdout:
    .venv\\Scripts\\python evaluate.py --data C:\\path\\to\\holdout
Prints a summary and writes per-window predictions next to --out.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from build_dataset import build_table
from motion_state.model import MotionStateModel
from run_experiments import debounce, event_metrics, metrics

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--model", type=Path, default=Path("models/motion_state_v1.pkl"))
    ap.add_argument("--out", type=Path, default=Path("outputs/eval"))
    args = ap.parse_args()

    table, _, _ = build_table(args.data, verbose=False)
    model = MotionStateModel.load(args.model)
    score = model.score(table)
    pred = score >= model.threshold
    result = metrics(table, pred, score)
    result["events_min_2s"] = event_metrics(table, debounce(table, pred, 4))

    args.out.mkdir(parents=True, exist_ok=True)
    table.assign(score=score, pred_dynamic=pred).drop(columns=[c for c in table.columns if c not in (
        "sample_id", "t_center", "label", "cls", "boundary")]).to_csv(args.out / "predictions.csv", index=False)
    (args.out / "result.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"recordings {table.sample_id.nunique()}, scored windows {result['windows']}")
    print(f"BAcc {result['bacc']}  AUC {result['auc']}  dynamic recall {result['dynamic_recall']}  "
          f"static recall {result['static_recall']}")
    print("share read as dynamic, by label group (lying/standing = false alarm):")
    for k, v in result["dynamic_rate_by_group"].items():
        print(f"  {k:24s} {v['rate']:.3f}  ({v['windows']} windows)")
    print("segments hit (any window):", result["events"]["dynamic_segments_hit"])
    print("with >=2 s minimum dynamic run:", result["events_min_2s"])
