"""Static/dynamic experiments on the cached window table.

Schemes
  lodo  : leave-one-date-out (0624 / 0625 / 0626 = place A, 0929 = place B)
Models
  thr   : one motion feature with a threshold (interpretable baseline)
  lr    : logistic regression on all motion features
  final : MotionStateModel = lr + causal per-recording relative features (the chosen model)
  gbm   : HistGradientBoosting on all motion features
Thresholds are set on the training dates so that STATIC_FA of static windows read dynamic;
this does not depend on which kinds of motion a date happens to contain.
Scoring uses windows whose centre is not ignore and is more than 1 s from a static/dynamic
change (labels are whole seconds).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import balanced_accuracy_score, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from motion_state.model import MotionStateModel

META = ["sample_id", "date", "person", "t_end", "t_center", "label", "cls", "dchg", "boundary"]
BASELINE_FEATURE = "lag3_2s_med"
HOP_SEC = 0.5
STATIC_FA = 0.03
DEBOUNCE = 2   # consecutive dynamic windows (1 s) that make one dynamic event
LABEL_GROUPS = {
    "walking": "walking", "falling": "falling",
    "getting_up": "getting_up/transition", "transition": "getting_up/transition",
    "slow_lying_down": "getting_up/transition",
    "bending_over": "bending/straightening", "straightening_up": "bending/straightening",
    "raising_arms": "arms/torso_turn", "lowering_arms": "arms/torso_turn", "turning_body": "arms/torso_turn",
    "turning_head": "head_turn", "lying": "lying", "standing": "standing",
    "tossing": "in_bed", "arm_movement": "in_bed", "small_movement": "in_bed",
}


def feature_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in META]


def scored(df: pd.DataFrame) -> pd.Series:
    return df.cls.isin(["static", "dynamic"]) & ~df.boundary


def balanced_weights(y: np.ndarray) -> np.ndarray:
    return np.where(y, 0.5 / y.mean(), 0.5 / (1 - y.mean()))


def fit_scorer(kind: str, train: pd.DataFrame, cols: list[str]):
    """Return a function mapping a window table to a dynamic score (higher = more dynamic)."""
    if kind == "thr":
        return lambda d: d[BASELINE_FEATURE].to_numpy()
    y = (train.cls == "dynamic").to_numpy()
    if kind == "lr":
        model = make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=3000))
        model.fit(train[cols], y, logisticregression__sample_weight=balanced_weights(y))
    else:
        model = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                               min_samples_leaf=40, l2_regularization=1.0, random_state=0)
        model.fit(train[cols], y, sample_weight=balanced_weights(y))
    return lambda d: model.predict_proba(d[cols])[:, 1]


def static_fa_threshold(score_static: np.ndarray, fa: float = STATIC_FA) -> float:
    return float(np.quantile(score_static, 1 - fa))


def debounce(df: pd.DataFrame, pred: np.ndarray, n: int = DEBOUNCE) -> np.ndarray:
    """A window stays dynamic only inside a run of >= n dynamic windows (per recording)."""
    out = np.zeros(len(pred), dtype=bool)
    s = pd.Series(pred, index=np.arange(len(pred)))
    for _, idx in df.reset_index(drop=True).groupby("sample_id").groups.items():
        p = s.loc[idx]
        run = (p != p.shift()).cumsum()
        size = p.groupby(run).transform("size")
        out[idx] = (p & (size >= n)).to_numpy()
    return out


def metrics(df: pd.DataFrame, pred: np.ndarray, score: np.ndarray) -> dict:
    m = scored(df).to_numpy()
    y = (df.cls == "dynamic").to_numpy()
    yt, yp = y[m], pred[m]
    sub = df[m].assign(pred=yp, group=df.label[m].map(LABEL_GROUPS).fillna(df.label[m]))
    by_group = sub.groupby("group").pred.agg(["mean", "size"])
    out = {
        "windows": int(m.sum()),
        "bacc": round(balanced_accuracy_score(yt, yp), 4),
        "auc": round(roc_auc_score(yt, score[m]), 4),
        "dynamic_recall": round(float(yp[yt].mean()), 4),
        "static_recall": round(float((~yp[~yt]).mean()), 4),
        # share of windows read as dynamic, per label group (for lying/standing this is the false-alarm rate)
        "dynamic_rate_by_group": {k: {"rate": round(v["mean"], 3), "windows": int(v["size"])}
                                  for k, v in by_group.iterrows()},
        "bacc_by_person": {p: round(balanced_accuracy_score(g.cls == "dynamic", g.pred), 3)
                           for p, g in sub.groupby("person") if g.cls.nunique() == 2},
    }
    out["events"] = event_metrics(df, pred)
    return out


def event_metrics(df: pd.DataFrame, pred: np.ndarray) -> dict:
    """Segment view: dynamic segments (>= 1 s) hit by any dynamic window, and false dynamic
    events (runs of dynamic windows) inside static segments, per hour of that posture."""
    d = df.reset_index(drop=True).assign(pred=pred)
    hits, total = {}, {}
    false_events = {"lying": 0, "standing": 0}
    static_sec = {"lying": 0.0, "standing": 0.0}
    for _, g in d.groupby("sample_id"):
        seg_id = (g.label != g.label.shift()).cumsum()
        for _, seg in g.groupby(seg_id):
            label = seg.label.iloc[0]
            if seg.cls.iloc[0] == "dynamic" and len(seg) * HOP_SEC >= 1:
                grp = LABEL_GROUPS.get(label, label)
                total[grp] = total.get(grp, 0) + 1
                hits[grp] = hits.get(grp, 0) + int(seg.pred.any())
            elif label in static_sec:
                inner = seg[~seg.boundary]
                static_sec[label] += len(inner) * HOP_SEC
                p = inner.pred.astype(int)
                false_events[label] += int(((p.diff().fillna(p) == 1)).sum())
    return {
        "dynamic_segments_hit": {k: f"{hits[k]}/{total[k]}" for k in sorted(total)},
        "false_events_per_hour": {k: round(false_events[k] / max(static_sec[k], 1) * 3600, 1) for k in static_sec},
        "static_minutes_scored": {k: round(v / 60, 1) for k, v in static_sec.items()},
    }


def run_fold(train: pd.DataFrame, test: pd.DataFrame, cols: list[str]) -> dict:
    tr = train[scored(train)]
    final = MotionStateModel().fit(train)
    s = final.score(test)
    out = {"final": metrics(test, s >= final.threshold, s) | {"threshold": round(final.threshold, 4)}}
    for kind in ("thr", "lr", "gbm"):
        f = fit_scorer(kind, tr, cols)
        thr = static_fa_threshold(f(tr[tr.cls == "static"]))
        s = f(test)
        out[kind] = metrics(test, s >= thr, s) | {"threshold": round(thr, 4)}
    return out


def summary_table(results: dict) -> pd.DataFrame:
    rows = []
    for name, fold in results.items():
        for model, m in fold.items():
            g = m["dynamic_rate_by_group"]
            rows.append({
                "test": name, "model": model, "bacc": m["bacc"], "auc": m["auc"],
                "dyn_rec": m["dynamic_recall"], "stat_rec": m["static_recall"],
                "walk": g.get("walking", {}).get("rate"), "fall": g.get("falling", {}).get("rate"),
                "getup": g.get("getting_up/transition", {}).get("rate"),
                "arms/turn": g.get("arms/torso_turn", {}).get("rate"),
                "lyingFA": g.get("lying", {}).get("rate"), "standFA": g.get("standing", {}).get("rate"),
                "lyingFE/h": m["events"]["false_events_per_hour"]["lying"],
                "standFE/h": m["events"]["false_events_per_hour"]["standing"],
            })
    return pd.DataFrame(rows)


def lying_scenario(df: pd.DataFrame) -> pd.DataFrame:
    """Sleepwalking view: static = lying only. Standing is dropped (the sujin_stand_normal
    recordings are labelled standing throughout but contain body turning)."""
    return df.assign(cls=df.cls.mask(df.label.eq("standing"), "ignore"))


def main(cache: Path, out: Path, scenario: str) -> None:
    df = pd.read_csv(cache / "windows.csv")
    df["date"] = df.date.astype(str)
    if scenario == "lying":
        df = lying_scenario(df)
    cols = feature_cols(df)
    results = {}
    for d in sorted(df.date.unique()):
        results[f"lodo:{d}"] = run_fold(df[df.date != d], df[df.date == d], cols)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    table = summary_table(results)
    table.to_csv(out / "summary.csv", index=False)
    print(table.to_string(index=False))
    print("\nmean over dates")
    print(table.groupby("model").mean(numeric_only=True).round(3).to_string())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=Path("outputs/cache"))
    ap.add_argument("--out", type=Path, default=Path("outputs/experiments"))
    ap.add_argument("--scenario", choices=["all", "lying"], default="all",
                    help="lying: static = lying only, standing excluded (sleepwalking view)")
    args = ap.parse_args()
    main(args.cache, args.out / args.scenario, args.scenario)
