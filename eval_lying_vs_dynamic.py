"""Static vs dynamic on real-time ticks. Static = lying (still + tossing), plus standing with
--standing-static; dynamic = everything else.

Leave-one-date-out: the threshold on the 2 s walk index (relative to the training lying
baseline, no calibration) is chosen on the other dates. Ticks within 1 s of a label change
are not scored (labels are whole seconds).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import balanced_accuracy_score

from fit_realtime import best_threshold, group

LYING = {"still", "tossing"}
TICK = 0.25


def label_change_distance(g: pd.DataFrame) -> np.ndarray:
    cls = g.cls.where(g.cls != "ignore").ffill().bfill().to_numpy()
    t = g.t.to_numpy()
    change = t[1:][cls[1:] != cls[:-1]]
    if not len(change):
        return np.full(len(t), 999.0)
    return np.abs(t[:, None] - change[None, :]).min(1)


def main(ticks: Path, out: Path, standing_static: bool = False) -> None:
    static = LYING | ({"standing"} if standing_static else set())
    df = pd.read_csv(ticks)
    df["date"] = df.date.astype(str)
    df["g"] = df.label_walk.map(group)
    df["cls"] = np.where(df.g == "ignore", "ignore", np.where(df.g.isin(static), "lying", "dynamic"))
    df["dchg"] = np.concatenate([label_change_distance(g) for _, g in df.groupby("sample_id", sort=False)])
    df = df.dropna(subset=["walk_raw"])                      # warm-up ticks have no index yet
    parts, thresholds = [], {}
    for d in sorted(df.date.unique()):
        train, test = df[df.date != d], df[df.date == d].copy()
        base = train[train.g == "still"].walk_raw.median()
        tr = train[(train.cls != "ignore") & (train.dchg > 1.0)]
        thr = best_threshold((tr.walk_raw - base).to_numpy(), (tr.cls == "dynamic").to_numpy())
        thresholds[d] = round(float(thr), 4)
        test["score"] = test.walk_raw - base
        test["pred"] = np.where(test.score >= thr, "dynamic", "lying")
        parts.append(test)
    res = pd.concat(parts)
    s = res[(res.cls != "ignore") & (res.dchg > 1.0)]
    y, p = s.cls == "dynamic", s.pred == "dynamic"

    report = {"thresholds_by_test_date": thresholds}
    report["overall"] = {
        "scored_sec": len(s) * TICK, "accuracy": round(float((y == p).mean()), 4),
        "balanced_accuracy": round(balanced_accuracy_score(y, p), 4),
        "dynamic_recall": round(float(p[y].mean()), 4), "lying_specificity": round(float((~p[~y]).mean()), 4),
        "missed_dynamic_sec": float((y & ~p).sum() * TICK), "false_dynamic_sec": float((~y & p).sum() * TICK),
    }
    report["by_date"] = {d: {"accuracy": round(float((g.cls == g.pred).mean()), 3),
                             "balanced_accuracy": round(balanced_accuracy_score(g.cls == "dynamic", g.pred == "dynamic"), 3),
                             "dynamic_recall": round(float((g[g.cls == "dynamic"].pred == "dynamic").mean()), 3),
                             "lying_specificity": round(float((g[g.cls == "lying"].pred == "lying").mean()), 3)}
                         for d, g in s.groupby("date")}
    report["by_label_rate_correct"] = {k: {"correct": round(float((g.pred == g.cls).mean()), 3), "sec": len(g) * TICK}
                                       for k, g in s.groupby("g")}

    # segment level: a dynamic segment is missed if none of its scored ticks is dynamic;
    # a lying segment has a false alarm run if >= 1 s of consecutive dynamic ticks
    seg_rows = []
    for sid, g in res.groupby("sample_id"):
        run = (g.g != g.g.shift()).cumsum()
        for _, seg in g.groupby(run):
            lab, cls = seg.g.iloc[0], seg.cls.iloc[0]
            inner = seg[seg.dchg > 1.0]
            if cls == "ignore" or inner.empty:
                continue
            dyn = (inner.pred == "dynamic").to_numpy()
            longest = max((len(list(v)) for k, v in __import__("itertools").groupby(dyn) if k), default=0) * TICK
            seg_rows.append({"sample_id": sid, "date": seg.date.iloc[0], "label": lab, "cls": cls,
                             "start": round(seg.t.iloc[0] - 1.0, 1), "end": round(seg.t.iloc[-1] - 1.0, 1),
                             "scored_sec": len(inner) * TICK, "dynamic_share": round(float(dyn.mean()), 3),
                             "longest_dynamic_run_sec": longest})
    seg = pd.DataFrame(seg_rows)
    dyn_seg, ly_seg = seg[seg.cls == "dynamic"], seg[seg.cls == "lying"]
    missed = dyn_seg[dyn_seg.dynamic_share == 0]
    false_seg = ly_seg[ly_seg.longest_dynamic_run_sec >= 1.0]
    report["segments"] = {
        "dynamic_segments": len(dyn_seg), "dynamic_missed_entirely": len(missed),
        "dynamic_missed_by_label": missed.label.value_counts().to_dict(),
        "dynamic_mostly_missed_(<50%)": int((dyn_seg.dynamic_share < 0.5).sum()),
        "lying_segments": len(ly_seg), "lying_segments_with_false_dynamic_>=1s": len(false_seg),
    }
    per_file = s.assign(miss=(s.cls == "dynamic") & (s.pred == "lying"), fa=(s.cls == "lying") & (s.pred == "dynamic")) \
        .groupby(["date", "sample_id"]).agg(scored_sec=("t", lambda x: len(x) * TICK),
                                            missed_sec=("miss", lambda x: x.sum() * TICK),
                                            false_sec=("fa", lambda x: x.sum() * TICK),
                                            accuracy=("cls", lambda x: 0.0)).reset_index()
    per_file["accuracy"] = 1 - (per_file.missed_sec + per_file.false_sec) / per_file.scored_sec
    out.mkdir(parents=True, exist_ok=True)
    per_file.sort_values("accuracy").to_csv(out / "per_file.csv", index=False)
    seg.to_csv(out / "segments.csv", index=False)
    (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")

    print(json.dumps({k: report[k] for k in ("thresholds_by_test_date", "overall", "by_date", "segments")},
                     indent=1, ensure_ascii=False))
    print("\ncorrect rate by label:")
    for k, v in sorted(report["by_label_rate_correct"].items(), key=lambda x: x[1]["correct"]):
        print(f"  {k:18s} {v['correct']:.3f}  ({v['sec']:.0f} s)")
    print("\nworst files:")
    print(per_file.sort_values("accuracy").head(15).round(3).to_string(index=False))
    print("\ndynamic segments missed entirely:")
    print(missed[["date", "sample_id", "label", "start", "end", "scored_sec"]].to_string(index=False))
    print("\nlying segments with >=1 s false dynamic:")
    print(false_seg[["date", "sample_id", "label", "start", "end", "dynamic_share", "longest_dynamic_run_sec"]]
          .to_string(index=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=Path, default=Path("outputs/cache/ticks.csv"))
    ap.add_argument("--out", type=Path, default=Path("outputs/lying_vs_dynamic"))
    ap.add_argument("--standing-static", action="store_true", help="count standing as static (with lying)")
    args = ap.parse_args()
    main(args.ticks, args.out / ("standing_static" if args.standing_static else "standing_dynamic"),
         args.standing_static)
