"""Fit and evaluate the real-time monitor on cached ticks (build_ticks.py), leave-one-date-out.

Baseline modes (service plan 10.3: report with and without install calibration)
  calibrated   : baseline = median index of still lying in the OTHER recordings of the same
                 date (same room and day), simulating the install-time lying test
  uncalibrated : one baseline from the training dates for every recording
Thresholds (walk_start, toss_threshold) come from the training dates only.
Event metrics follow service plan section 11.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import balanced_accuracy_score

from motion_state.realtime import MonitorConfig, NightMonitor

IN_BED = {"tossing", "arm_movement", "small_movement"}
STATIC_GROUPS = {"still", "tossing"}
OK_SIGNAL = {"ok": True, "packet_rate": {}, "reason": None}


def group(label: str) -> str:
    if label == "lying":
        return "still"
    if label in IN_BED:
        return "tossing"
    if label in ("ignore", "unlabeled", "label"):
        return "ignore"
    return label


def best_threshold(x: np.ndarray, y: np.ndarray) -> float:
    grid = np.quantile(x, np.linspace(0.01, 0.99, 197))
    return float(max(grid, key=lambda v: balanced_accuracy_score(y, x >= v)))


def baselines(df: pd.DataFrame, mode: str, train: pd.DataFrame) -> dict[str, dict]:
    still_train = train[train.g_walk == "still"]
    glob = {"toss": float(still_train.toss_raw.median()), "walk": float(still_train.walk_raw.median())}
    out = {}
    for sid, g in df.groupby("sample_id"):
        if mode == "calibrated":
            same_day = df[(df.date == g.date.iloc[0]) & (df.sample_id != sid) & (df.g_walk == "still")]
            if len(same_day) >= 40:   # >= 10 s of quiet lying elsewhere in that room/day
                out[sid] = {"toss": float(same_day.toss_raw.median()), "walk": float(same_day.walk_raw.median())}
                continue
        out[sid] = glob
    return out


def relative(df: pd.DataFrame, base: dict[str, dict]) -> pd.DataFrame:
    b = df.sample_id.map(base)
    return df.assign(toss_rel=df.toss_raw - b.map(lambda x: x["toss"]),
                     walk_rel=df.walk_raw - b.map(lambda x: x["walk"]))


def fit(train: pd.DataFrame, mode: str, fallback_toss: float) -> dict:
    tr = relative(train, baselines(train, mode, train)).dropna(subset=["walk_rel"])
    w = tr[tr.g_walk.isin(["walking", "still", "tossing"])]
    walk_start = max(best_threshold(w.walk_rel.to_numpy(), (w.g_walk == "walking").to_numpy()), 0.15)
    t = tr[tr.g_toss.isin(["still", "tossing"])]
    if (t.g_toss == "tossing").sum() >= 20:
        toss = best_threshold(t.toss_rel.to_numpy(), (t.g_toss == "tossing").to_numpy())
    else:
        toss = fallback_toss
    return {"walk_start": round(walk_start, 4), "walk_end": round(0.75 * walk_start, 4),
            "toss_threshold": round(toss, 4)}


def run(test: pd.DataFrame, cfg: MonitorConfig, base: dict[str, dict]) -> pd.DataFrame:
    rows = []
    for sid, g in test.groupby("sample_id"):
        mon = NightMonitor(cfg, base[sid])
        for r in g.itertuples():
            idx = None if np.isnan(r.toss_raw) else (r.toss_raw, r.walk_raw)
            o = mon.decide(r.t, idx, OK_SIGNAL)
            rows.append({"sample_id": sid, "t": r.t, "g_walk": r.g_walk, "g_toss": r.g_toss,
                         "internal": o.internal, "state": o.state, "alert": o.alert is not None})
    return pd.DataFrame(rows)


def segments(g: pd.DataFrame, col: str = "g_walk"):
    run_id = (g[col] != g[col].shift()).cumsum()
    for _, s in g.groupby(run_id):
        yield s[col].iloc[0], float(s.t.iloc[0]), float(s.t.iloc[-1])


def evaluate(res: pd.DataFrame, tol: float = 2.0) -> dict:
    walk_hit, walk_total, latency, latency_clean, warm_missing = 0, 0, [], [], 0
    false_alerts, standing_alerts, justified = 0, 0, 0
    for sid, g in res.groupby("sample_id"):
        alerts = g[g.alert].t.to_numpy()
        for kind, a, b in segments(g):
            if kind != "walking":
                continue
            if (g[(g.t >= a) & (g.t <= b)].internal == "unknown").all():
                warm_missing += 1      # whole walk inside the warm-up period
                continue
            walk_total += 1
            valid = g[(g.t >= a) & (g.t <= b) & (g.internal != "unknown")].t
            start = max(a - 1.0, float(valid.iloc[0]) - 1.0)   # label time = window centre; skip warm-up
            after_warmup = (g[g.t <= a].internal != "unknown").any()
            hit = alerts[(alerts >= a) & (alerts <= b + tol)]
            # an alert raised earlier in the same continuing event also covers this walk
            ongoing = g[(g.t >= a) & (g.t <= b)].state.eq("sustained_activity").any()
            if len(hit) or ongoing:
                walk_hit += 1
                if len(hit):
                    latency.append(float(hit[0] - start))
                    if after_warmup:
                        latency_clean.append(float(hit[0] - start))
        for at in alerts:
            near = set(g[(g.t >= at - 3) & (g.t <= at + tol)].g_walk) - {"ignore"}
            if not near:
                continue                # alert inside an ignored span (e.g. another person moving)
            if near - STATIC_GROUPS - {"standing"}:
                justified += 1
            elif "standing" in near:
                standing_alerts += 1
            else:
                false_alerts += 1
    lying_hours = res.g_walk.isin(STATIC_GROUPS).sum() * 0.25 / 3600
    return {
        "walk_events_alerted": f"{walk_hit}/{walk_total}",
        "walk_recall": round(walk_hit / max(walk_total, 1), 3),
        "walks_inside_warmup": warm_missing,
        "latency_from_walk_start_sec": {"median": round(float(np.median(latency)), 2) if latency else None,
                                        "p95": round(float(np.percentile(latency, 95)), 2) if latency else None},
        "latency_walks_starting_after_warmup_sec": {
            "n": len(latency_clean),
            "median": round(float(np.median(latency_clean)), 2) if latency_clean else None,
            "p95": round(float(np.percentile(latency_clean, 95)), 2) if latency_clean else None},
        "alerts_justified": justified,
        "alerts_during_standing": standing_alerts,
        "false_alerts_lying_or_tossing": false_alerts,
        "lying_minutes": round(lying_hours * 60, 1),
        "false_alerts_per_8h_lying": round(false_alerts / max(lying_hours, 1e-9) * 8, 1),
    }


def internal_confusion(res: pd.DataFrame) -> dict:
    # true walk/still from the 2 s window label; tossing from the 1 s window label
    true = np.where(res.g_toss == "tossing", "tossing", res.g_walk)
    keep = np.isin(true, ["still", "tossing", "walking"]) & (res.internal != "unknown")
    cm = pd.crosstab(pd.Series(true[keep], name="true"), pd.Series(res.internal[keep].to_numpy(), name="pred"),
                     normalize="index").round(3)
    return cm.to_dict("index")


def main(ticks: Path, out: Path) -> None:
    df = pd.read_csv(ticks)
    df["date"] = df.date.astype(str)
    df["g_walk"] = df.label_walk.map(group)
    df["g_toss"] = df.label_toss.map(group)
    # tossing labels exist only on 0624; for that fold the threshold falls back to a value
    # fitted on 0624 itself, so tossing numbers on 0624 are not out-of-sample
    fallback = fit(df[df.date == "20260624"], "calibrated", 0.06)["toss_threshold"]
    report = {}
    variants = {
        "A_continuous_3s": dict(persistence="continuous", walk_hold_sec=3.0),
        "A_continuous_5s": dict(persistence="continuous", walk_hold_sec=5.0),
        "B_ratio_8s_70pct": dict(persistence="ratio", ratio_window_sec=8.0, ratio_min=0.7),
    }
    for mode in ("calibrated", "uncalibrated"):
        for vname, vcfg in variants.items():
            parts, fitted = [], {}
            for d in sorted(df.date.unique()):
                train, test = df[df.date != d], df[df.date == d]
                th = fit(train, mode, fallback)
                fitted[d] = th
                cfg = replace(MonitorConfig(), **th, **vcfg)
                parts.append(run(test, cfg, baselines(test, mode, train)))
            res = pd.concat(parts, ignore_index=True)
            key = f"{mode}/{vname}"
            report[key] = {"events": evaluate(res), "internal_confusion": internal_confusion(res),
                           "thresholds_by_test_date": fitted}
            e = report[key]["events"]
            print(f"{key:36s} walks alerted {e['walk_events_alerted']:>6} | latency med {e['latency_from_walk_start_sec']['median']}s "
                  f"p95 {e['latency_from_walk_start_sec']['p95']}s (after warm-up n={e['latency_walks_starting_after_warmup_sec']['n']} med {e['latency_walks_starting_after_warmup_sec']['median']}s) | false alerts (lying/tossing) {e['false_alerts_lying_or_tossing']} "
                  f"over {e['lying_minutes']} min | standing {e['alerts_during_standing']} | justified {e['alerts_justified']}")
    final = fit(df, "calibrated", fallback)
    cfg = replace(MonitorConfig(), **final, **variants["A_continuous_3s"])
    Path("models").mkdir(exist_ok=True)
    cfg.save(Path("models/realtime_config.json"))
    print(f"saved models/realtime_config.json (fitted on all dates): {final}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "realtime_eval.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\ninternal state confusion (calibrated, A 3 s):")
    print(pd.DataFrame(report["calibrated/A_continuous_3s"]["internal_confusion"]).T.to_string())
    print("\ninternal state confusion (uncalibrated, A 3 s):")
    print(pd.DataFrame(report["uncalibrated/A_continuous_3s"]["internal_confusion"]).T.to_string())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=Path, default=Path("outputs/cache/ticks.csv"))
    ap.add_argument("--out", type=Path, default=Path("outputs/realtime"))
    args = ap.parse_args()
    main(args.ticks, args.out)
