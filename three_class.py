"""Three-class evaluation: still lying / tossing (in-bed movement) / walking.

Every other action and standing is excluded from training and scoring. Nested
leave-one-group-out: model and feature set chosen on inner folds of the training groups only;
decision = argmax of balanced-weight class probabilities. Windows within 0.5 s of a change
between any two of the three classes are not scored.

Tossing exists only in two 0624 recordings (hoyeon, sujin). With --group date the 0624 fold
has never seen tossing, so --group person (hoyeon <-> sujin) is the meaningful test.
"""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import balanced_accuracy_score, confusion_matrix
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

IN_BED = {"tossing", "arm_movement", "small_movement"}
CLASSES = ["still", "tossing", "walking"]
META = ["sample_id", "date", "person", "label", "cls", "dchg", "t", "y"]


def three(label: str) -> str | None:
    if label == "lying":
        return "still"
    if label in IN_BED:
        return "tossing"
    if label == "walking":
        return "walking"
    return None


def change_dist(g: pd.DataFrame) -> np.ndarray:
    y = g.y.fillna("other").to_numpy()
    t = g.t.to_numpy()
    ch = t[1:][y[1:] != y[:-1]]
    return np.abs(t[:, None] - ch[None, :]).min(1) if len(ch) else np.full(len(t), 999.0)


MODELS = {
    "lr": lambda: make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=4000)),
    "gbm": lambda: HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_depth=3,
                                                  min_samples_leaf=40, l2_regularization=1.0, random_state=0),
}


def fit(kind, X, y):
    counts = pd.Series(y).value_counts()
    w = pd.Series(y).map(lambda c: len(y) / (len(counts) * counts[c])).to_numpy()
    m = MODELS[kind]()
    if kind == "lr":
        m.fit(X, y, logisticregression__sample_weight=w)
    else:
        m.fit(X, y, sample_weight=w)
    return m


def predict(m, X) -> np.ndarray:
    return m.classes_[m.predict_proba(X).argmax(1)]


def main(table: Path, group: str, out: Path) -> None:
    df = pd.read_csv(table)
    df["y"] = df.label.map(three)
    df["dchg3"] = np.concatenate([change_dist(g) for _, g in df.groupby("sample_id", sort=False)])
    feats = [c for c in df.columns if c not in META + ["dchg3"]]
    fsets = {"abs": [c for c in feats if not c.startswith("rel_")], "abs+rel": feats}
    use = df[df.y.notna() & (df.dchg3 > 0.5)].copy()
    if group == "person":
        use = use[use.person.isin(["hoyeon", "sujin", "yena", "csi"])]
    groups = sorted(g for g in use[group].unique() if g != "csi")
    preds, chosen = [], {}
    for g in groups:
        train, test = use[use[group] != g], use[use[group] == g].copy()
        inner_groups = [x for x in sorted(train[group].unique()) if x != "csi"]
        best = None
        for (fname, cols), kind in itertools.product(fsets.items(), MODELS):
            oof_t, oof_p = [], []
            for ig in inner_groups:
                itr, iva = train[train[group] != ig], train[train[group] == ig]
                if itr.y.nunique() < 2:
                    continue
                m = fit(kind, itr[cols], itr.y.to_numpy())
                oof_t.append(iva.y.to_numpy())
                oof_p.append(predict(m, iva[cols]))
            score = balanced_accuracy_score(np.concatenate(oof_t), np.concatenate(oof_p))
            if best is None or score > best[0]:
                best = (score, fname, kind)
        _, fname, kind = best
        m = fit(kind, train[fsets[fname]], train.y.to_numpy())
        test["pred"] = predict(m, test[fsets[fname]])
        test["fold"] = g
        preds.append(test)
        chosen[str(g)] = {"features": fname, "model": kind, "inner_bacc": round(best[0], 3),
                          "train_has_tossing": bool((train.y == "tossing").any())}
        print(f"  {group}={g}: {chosen[str(g)]}", flush=True)
    res = pd.concat(preds)
    cm = confusion_matrix(res.y, res.pred, labels=CLASSES)
    rep = {
        "chosen": chosen,
        "seconds_per_class": {c: float((res.y == c).sum() * 0.5) for c in CLASSES},
        "accuracy": round(float((res.y == res.pred).mean()), 4),
        "balanced_accuracy": round(balanced_accuracy_score(res.y, res.pred), 4),
        "recall": {c: round(float(cm[i, i] / max(cm[i].sum(), 1)), 3) for i, c in enumerate(CLASSES)},
        "confusion_rows_true_cols_pred": {c: dict(zip(CLASSES, (cm[i] / max(cm[i].sum(), 1)).round(3).tolist()))
                                          for i, c in enumerate(CLASSES)},
        "by_fold": {str(f): {"accuracy": round(float((g.y == g.pred).mean()), 3),
                             "recall": {c: round(float((g[g.y == c].pred == c).mean()), 3) for c in CLASSES
                                        if (g.y == c).any()}}
                    for f, g in res.groupby("fold")},
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / f"three_class_{group}.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    res[["sample_id", "t", "label", "y", "pred", "fold"]].to_csv(out / f"three_class_{group}_pred.csv", index=False)
    print(json.dumps({k: rep[k] for k in rep if k != "chosen"}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", type=Path, default=Path("outputs/v2/windows_L1_toss-static_stand-exclude_walkonly.csv"))
    ap.add_argument("--group", choices=["date", "person"], default="person")
    ap.add_argument("--out", type=Path, default=Path("outputs/v2/three_class"))
    args = ap.parse_args()
    main(args.table, args.group, args.out)
