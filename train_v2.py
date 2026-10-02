"""Nested cross-validation for lying(static) vs dynamic, then the final model.

Outer split: leave-one-date-out (or --group person: leave-one-person-out).
Inner split: leave-one-group-out over the outer-training groups only. Everything that is
chosen (feature set, model, smoothing, threshold) is chosen from inner out-of-fold
predictions. The outer test group is predicted once with that choice.

Scored windows: cls in {static, dynamic} and more than 0.5 s from a static/dynamic label
change (labels have 1 s resolution). Training uses the same windows.
"""
from __future__ import annotations

import argparse
import itertools
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import balanced_accuracy_score, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

META = ["sample_id", "date", "person", "label", "cls", "dchg", "t"]
BOUNDARY_SEC = 0.5
IN_BED = {"tossing", "arm_movement", "small_movement"}


def feature_sets(df: pd.DataFrame) -> dict[str, list[str]]:
    feats = [c for c in df.columns if c not in META]
    absf = [c for c in feats if not c.startswith("rel_")]
    rel = [c for c in feats if c.startswith("rel_")]
    short = [c for c in absf if "_w40" not in c and not c.startswith("shift")]
    return {"abs": absf, "rel": rel, "abs+rel": absf + rel,
            "short_abs+rel": short + [f"rel_{c}" for c in short]}


MODELS = {
    "lr": lambda: make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=4000)),
    "gbm": lambda: HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_depth=3,
                                                  min_samples_leaf=80, l2_regularization=1.0, random_state=0),
}
SMOOTH = (1, 3)   # causal mean of the last n window probabilities


def usable(df: pd.DataFrame) -> pd.Series:
    return df.cls.isin(["static", "dynamic"]) & (df.dchg > BOUNDARY_SEC)


def fit_model(kind: str, X: pd.DataFrame, y: np.ndarray):
    w = np.where(y, 0.5 / y.mean(), 0.5 / (1 - y.mean()))
    m = MODELS[kind]()
    if kind == "lr":
        m.fit(X, y, logisticregression__sample_weight=w)
    else:
        m.fit(X, y, sample_weight=w)
    return m


def predict(m, df: pd.DataFrame, cols: list[str], smooth: int) -> np.ndarray:
    p = pd.Series(m.predict_proba(df[cols])[:, 1], index=df.index)
    if smooth > 1:
        p = p.groupby(df.sample_id).transform(lambda x: x.rolling(smooth, min_periods=1).mean())
    return p.to_numpy()


def best_threshold(p: np.ndarray, y: np.ndarray) -> float:
    grid = np.unique(np.quantile(p, np.linspace(0.01, 0.99, 197)))
    return float(max(grid, key=lambda v: balanced_accuracy_score(y, p >= v)))


def select_config(train: pd.DataFrame, group: str, fsets: dict) -> dict:
    """Inner leave-one-group-out over the training groups; pick by balanced accuracy."""
    groups = sorted(train[group].unique())
    results = []
    for (fname, cols), kind, smooth in itertools.product(fsets.items(), MODELS, SMOOTH):
        oof_p, oof_y = [], []
        for g in groups:
            tr, va = train[train[group] != g], train[train[group] == g]
            trm = tr[usable(tr)]
            m = fit_model(kind, trm[cols], (trm.cls == "dynamic").to_numpy())
            p = predict(m, va, cols, smooth)
            keep = usable(va).to_numpy()
            oof_p.append(p[keep])
            oof_y.append((va.cls == "dynamic").to_numpy()[keep])
        p, y = np.concatenate(oof_p), np.concatenate(oof_y)
        thr = best_threshold(p, y)
        results.append({"features": fname, "model": kind, "smooth": smooth, "threshold": thr,
                        "inner_bacc": balanced_accuracy_score(y, p >= thr)})
    return max(results, key=lambda r: r["inner_bacc"]) | {"all_inner": results}


def outer(df: pd.DataFrame, group: str) -> tuple[pd.DataFrame, dict]:
    fsets = feature_sets(df)
    preds, chosen = [], {}
    for g in sorted(df[group].unique()):
        train, test = df[df[group] != g], df[df[group] == g].copy()
        if train[group].nunique() < 2:
            continue
        cfg = select_config(train, group, fsets)
        cols = fsets[cfg["features"]]
        trm = train[usable(train)]
        m = fit_model(cfg["model"], trm[cols], (trm.cls == "dynamic").to_numpy())
        test["p"] = predict(m, test, cols, cfg["smooth"])
        test["pred"] = np.where(test.p >= cfg["threshold"], "dynamic", "static")
        test["fold"] = g
        preds.append(test)
        chosen[g] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in cfg.items() if k != "all_inner"}
        print(f"  outer {group}={g}: chose {chosen[g]}", flush=True)
    return pd.concat(preds), chosen


def report(res: pd.DataFrame, n_boot: int = 1000) -> dict:
    s = res[usable(res)]
    y, p = (s.cls == "dynamic").to_numpy(), (s.pred == "dynamic").to_numpy()

    def stats(y, p, prob=None):
        out = {"seconds": len(y) * 0.5, "accuracy": round(float((y == p).mean()), 4),
               "balanced_accuracy": round(balanced_accuracy_score(y, p), 4),
               "dynamic_recall": round(float(p[y].mean()), 4), "static_correct": round(float((~p[~y]).mean()), 4)}
        if prob is not None and len(set(y)) == 2:
            out["auc"] = round(roc_auc_score(y, prob), 4)
        return out

    rep = {"overall": stats(y, p, s.p.to_numpy())}
    # bootstrap over recordings (windows of one recording are not independent)
    rng = np.random.default_rng(0)
    sids = s.sample_id.unique()
    by = {k: g for k, g in s.groupby("sample_id")}
    accs, baccs = [], []
    for _ in range(n_boot):
        b = pd.concat([by[k] for k in rng.choice(sids, len(sids))])
        yy, pp = (b.cls == "dynamic").to_numpy(), (b.pred == "dynamic").to_numpy()
        accs.append((yy == pp).mean())
        if len(set(yy)) == 2:
            baccs.append(balanced_accuracy_score(yy, pp))
    rep["overall"]["accuracy_95ci"] = [round(float(np.percentile(accs, q)), 4) for q in (2.5, 97.5)]
    rep["overall"]["bacc_95ci"] = [round(float(np.percentile(baccs, q)), 4) for q in (2.5, 97.5)]
    rep["by_fold"] = {str(f): stats((g.cls == "dynamic").to_numpy(), (g.pred == "dynamic").to_numpy(), g.p.to_numpy())
                      for f, g in s.groupby("fold")}
    lab = s.label.where(~s.label.isin(IN_BED), "tossing(in-bed)")
    rep["by_label_correct"] = {k: {"correct": round(float((g.pred == g.cls).mean()), 3), "sec": len(g) * 0.5,
                                   "class": g.cls.iloc[0]} for k, g in s.groupby(lab)}
    # segment view on all labelled windows (boundary windows included)
    seg_rows = []
    for sid, g in res[res.cls != "ignore"].groupby("sample_id"):
        run = (g.label != g.label.shift()).cumsum()
        for _, sg in g.groupby(run):
            dyn = (sg.pred == "dynamic").to_numpy()
            longest = max((len(list(v)) for k, v in itertools.groupby(dyn) if k), default=0) * 0.5
            seg_rows.append({"sample_id": sid, "fold": sg.fold.iloc[0], "label": sg.label.iloc[0], "cls": sg.cls.iloc[0],
                             "start": sg.t.iloc[0], "end": sg.t.iloc[-1] + 0.5, "dur": len(sg) * 0.5,
                             "dynamic_share": round(float(dyn.mean()), 3), "longest_dynamic_run": longest})
    seg = pd.DataFrame(seg_rows)
    dseg = seg[(seg.cls == "dynamic") & (seg.dur >= 1.0)]
    sseg = seg[(seg.cls == "static") & (seg.dur >= 2.0)]
    rep["segments"] = {
        "dynamic_segments(>=1s)": len(dseg),
        "dynamic_detected(any window)": int((dseg.dynamic_share > 0).sum()),
        "dynamic_missed": dseg[dseg.dynamic_share == 0][["sample_id", "label", "start", "dur"]].to_dict("records"),
        "static_segments(>=2s)": len(sseg),
        "static_with_false_run>=1.5s": int((sseg.longest_dynamic_run >= 1.5).sum()),
        "static_false_runs": sseg[sseg.longest_dynamic_run >= 1.5][
            ["sample_id", "label", "start", "dur", "longest_dynamic_run"]].to_dict("records"),
    }
    return rep


def lookahead_of(table: Path) -> float:
    """windows_L1_xxx.csv -> 1.0"""
    return float(table.stem.split("_L")[1].split("_")[0])


def main(table: Path, group: str, out: Path, save_final: Path | None) -> None:
    df = pd.read_csv(table)
    df["date"] = df.date.astype(str)
    if group == "person":
        df = df[df.person.isin(["hoyeon", "sujin", "yena"]) | (df.person == "csi")]
        df.loc[df.person == "csi", "person"] = "csi_always_train"
    print(f"== {table.name}, outer groups: {group}", flush=True)
    res, chosen = outer(df if group != "person" else df, group)
    if group == "person":
        res = res[res.fold != "csi_always_train"]
    rep = report(res)
    rep["chosen_per_fold"] = chosen
    out.mkdir(parents=True, exist_ok=True)
    tag = f"{table.stem}_{group}"
    (out / f"report_{tag}.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    res.drop(columns=[c for c in res.columns if c not in META + ["p", "pred", "fold"]]).to_csv(
        out / f"predictions_{tag}.csv", index=False)
    print(json.dumps({k: rep[k] for k in ("overall", "by_fold")}, indent=1))
    if save_final:
        # final model: config chosen by leave-one-date-out CV over ALL dates, trained on all data
        fsets = feature_sets(df)
        cfg = select_config(df, group, fsets)
        cols = fsets[cfg["features"]]
        m = fit_model(cfg["model"], df[usable(df)][cols], (df[usable(df)].cls == "dynamic").to_numpy())
        save_final.parent.mkdir(parents=True, exist_ok=True)
        save_final.write_bytes(pickle.dumps({"model": m, "columns": cols, "smooth": cfg["smooth"],
                                             "threshold": cfg["threshold"], "features": cfg["features"],
                                             "kind": cfg["model"], "lookahead_table": table.name,
                                             "lookahead_sec": lookahead_of(table)}))
        print(f"saved final model {save_final}: {cfg['features']}/{cfg['model']}/smooth{cfg['smooth']} "
              f"thr={cfg['threshold']:.3f} (CV bacc {cfg['inner_bacc']:.3f})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", type=Path, default=Path("outputs/v2/windows_L0.csv"))
    ap.add_argument("--group", choices=["date", "person"], default="date")
    ap.add_argument("--out", type=Path, default=Path("outputs/v2"))
    ap.add_argument("--save-final", type=Path)
    args = ap.parse_args()
    main(args.table, args.group, args.out, args.save_final)
