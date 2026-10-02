"""Service-level evaluation (plan section 11) of NightMonitorV2, leave-one-date-out.

For each held-out date: a fold model is trained on the other dates with the configuration that
nested CV chose for that fold (train_v2.py report), every recording of the held-out date is
replayed packet by packet through the streaming detector, and the service state machine runs
on those decisions. Persistence rules are compared on the same decisions.

Metrics
  walk event recall : walking segments with an alert in [start, end + 2 s] or an ongoing event
  latency           : walk start -> alert output (decision moment + 1 s look-ahead)
  false alerts      : alerts with only lying / tossing within [-3 s, +2 s]   (per 8 h of lying)
  standing alerts   : alerts with only standing still around
  activity alerts   : alerts during other real activity (getting up, falls, arm movement, ...)
"""
from __future__ import annotations

import argparse
import json
import pickle
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from build_v2 import IN_BED
from motion_state.detector_v2 import Decision, LyingDynamicDetector
from motion_state.features import frame_labels, load_labels
from motion_state.monitor_v2 import NightMonitorV2, ServiceConfig
from motion_state.replay import iter_packets
from train_v2 import feature_sets, fit_model, usable

OK = {"ok": True, "packet_rate": {}, "reason": None}
VARIANTS = {
    "A_2s": ServiceConfig(persistence="continuous", hold_sec=2.0),
    "A_3s": ServiceConfig(persistence="continuous", hold_sec=3.0),
    "A_5s": ServiceConfig(persistence="continuous", hold_sec=5.0),
    "B_8s_70%": ServiceConfig(persistence="ratio", ratio_window_sec=8.0, ratio_min=0.7),
}


def group(label: str) -> str:
    if label == "lying" or label in IN_BED:
        return "lying"
    if label in ("walking", "standing"):
        return label
    if label in ("ignore", "unlabeled", "label"):
        return "ignore"
    return "other_activity"


def stream_decisions(raw: Path, model: Path) -> list[Decision]:
    det, decs, last = LyingDynamicDetector(model), [], 0.0
    for t, rx, a in iter_packets(raw):
        det.push_packet(t, rx, a)
        decs += det.poll(t)
        last = t
    return decs + det.poll(last + 2.0)


def run_monitor(decs: list[Decision], model: Path, cfg: ServiceConfig) -> pd.DataFrame:
    mon = NightMonitorV2(model, cfg)
    rows = [mon.on_decision(d, OK).to_dict() for d in decs]
    return pd.DataFrame(rows)


def evaluate(res: pd.DataFrame, lookahead: float, hold: float, tol: float = 2.0) -> dict:
    """Walk events are split by whether their start was observed after the detector warm-up
    (the realistic lying -> walking case) or fell inside it (clip starts mid-walk)."""
    out = {"walks_after_warmup": 0, "alerted_after_warmup": 0,
           "walks_from_warmup": 0, "alerted_from_warmup": 0, "too_short_after_warmup": 0,
           "false_alerts": 0, "standing_alerts": 0, "activity_alerts": 0, "walk_alerts": 0, "alerts": 0,
           "tossing_segments": 0, "tossing_segments_alerted": 0}
    lat_after, lat_all, false_list, missed = [], [], [], []
    for sid, g in res.groupby("sample_id"):
        g = g.reset_index(drop=True)
        alerts = g[g.alert.notna()].t.to_numpy()
        valid_t = g[g.state != "baseline_prep"].t
        first_valid = float(valid_t.iloc[0]) if len(valid_t) else np.inf
        run = (g.g != g.g.shift()).cumsum()
        for _, seg in g.groupby(run):
            a, b = seg.t.iloc[0], seg.t.iloc[-1]
            if seg.g.iloc[0] != "walking":
                continue
            start = max(a, first_valid)
            if b - start + 0.5 < hold:          # not enough observable walking for the hold rule
                out["too_short_after_warmup"] += 1
                continue
            hit_alerts = alerts[(alerts >= a - 0.5) & (alerts <= b + tol)]
            hit = bool(len(hit_alerts)) or (seg.state == "sustained_activity").any()
            key = "after_warmup" if a >= first_valid else "from_warmup"
            out[f"walks_{key}"] += 1
            out[f"alerted_{key}"] += int(hit)
            if not hit:
                missed.append({"sample_id": sid, "walk_start": float(a), "walk_end": float(b + 0.5)})
            if len(hit_alerts):
                lat = float(hit_alerts[0] + lookahead - start)
                lat_all.append(lat)
                if key == "after_warmup":
                    lat_after.append(lat)
        lrun = (g.label != g.label.shift()).cumsum()
        for _, seg in g.groupby(lrun):
            if seg.label.iloc[0] in IN_BED:
                a, b = seg.t.iloc[0], seg.t.iloc[-1]
                out["tossing_segments"] += 1
                out["tossing_segments_alerted"] += int(((alerts >= a) & (alerts <= b + 1.0)).any())
        for at in alerts:
            out["alerts"] += 1
            near = set(g[(g.t >= at - 3) & (g.t <= at + tol)].g) - {"ignore"}
            if not near:
                continue
            if "walking" in near:
                out["walk_alerts"] += 1
            elif near - {"lying", "standing"}:
                out["activity_alerts"] += 1
            elif "standing" in near:
                out["standing_alerts"] += 1
            else:
                out["false_alerts"] += 1
                false_list.append({"sample_id": sid, "alert_t": float(at),
                                   "labels": sorted(set(g[(g.t >= at - 3) & (g.t <= at + tol)].label))})
    lying_h = (res[res.state != "baseline_prep"].g == "lying").sum() * 0.5 / 3600
    q = lambda v, p: round(float(np.percentile(v, p)), 2) if v else None
    out.update({
        "latency_after_warmup_sec": {"median": q(lat_after, 50), "p95": q(lat_after, 95), "n": len(lat_after)},
        "latency_all_sec": {"median": q(lat_all, 50), "p95": q(lat_all, 95), "n": len(lat_all)},
        "lying_minutes": round(lying_h * 60, 1),
        "false_alerts_per_8h_lying": round(out["false_alerts"] / max(lying_h, 1e-9) * 8, 1),
        "false_alert_list": false_list, "missed_walks": missed,
    })
    return out


def main(table: Path, report: Path, data: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(table)
    df["date"] = df.date.astype(str)
    chosen = json.loads(report.read_text(encoding="utf-8"))["chosen_per_fold"]
    fsets = feature_sets(df)
    raws = {p.name.removesuffix("_csi_raw.csv"): p for p in data.glob("raw_csi/**/*_csi_raw.csv")}
    all_dec = []
    for d, cfg in chosen.items():
        cache = out / f"decisions_{d}.csv"
        model_path = out / f"fold_{d}.pkl"
        if not model_path.exists():
            train = df[(df.date != d) & usable(df)]
            cols = fsets[cfg["features"]]
            m = fit_model(cfg["model"], train[cols], (train.cls == "dynamic").to_numpy())
            model_path.write_bytes(pickle.dumps({"model": m, "columns": cols, "smooth": cfg["smooth"],
                                                 "threshold": cfg["threshold"], "lookahead_sec": 1.0}))
        if not cache.exists():
            rows = []
            for sid in sorted(df[df.date == d].sample_id.unique()):
                for x in stream_decisions(raws[sid], model_path):
                    rows.append({"sample_id": sid, "date": d, **x.__dict__})
                print(f"  {d} {sid} streamed", flush=True)
            pd.DataFrame(rows).to_csv(cache, index=False)
        all_dec.append(pd.read_csv(cache))
    dec = pd.concat(all_dec, ignore_index=True)
    labels = {sid: load_labels(next(data.glob(f"labels/**/{sid}_labels.csv"))) for sid in dec.sample_id.unique()}

    results = {}
    for name, scfg in VARIANTS.items():
        parts = []
        for (d, sid), g in dec.groupby(["date", "sample_id"]):
            decs = [Decision(r.t, r.state, None if pd.isna(r.probability) else r.probability,
                             None if pd.isna(r.motion) else r.motion) for r in g.itertuples()]
            r = run_monitor(decs, out / f"fold_{d}.pkl", scfg)
            lab = frame_labels(r.t.to_numpy(), labels[sid])
            parts.append(r.assign(sample_id=sid, date=d, label=lab, g=[group(x) for x in lab]))
        res = pd.concat(parts, ignore_index=True)
        hold = scfg.hold_sec if scfg.persistence == "continuous" else scfg.ratio_window_sec * scfg.ratio_min
        results[name] = {"all": evaluate(res, 1.0, hold),
                         "by_date": {d: {k: v for k, v in evaluate(g, 1.0, hold).items()
                                         if k not in ("false_alert_list", "missed_walks")}
                                     for d, g in res.groupby("date")}}
        if name == "A_3s":
            res.to_csv(out / "monitor_A3s.csv", index=False)
        e = results[name]["all"]
        print(f"{name:9s} walks after warm-up {e['alerted_after_warmup']}/{e['walks_after_warmup']} "
              f"(latency med {e['latency_after_warmup_sec']['median']}s p95 {e['latency_after_warmup_sec']['p95']}s) | "
              f"walks from warm-up {e['alerted_from_warmup']}/{e['walks_from_warmup']} | too short {e['too_short_after_warmup']} | "
              f"false {e['false_alerts']} in {e['lying_minutes']} min lying ({e['false_alerts_per_8h_lying']}/8h) | "
              f"standing {e['standing_alerts']} | other activity {e['activity_alerts']} | "
              f"tossing segs alerted {e['tossing_segments_alerted']}/{e['tossing_segments']}", flush=True)
    (out / "service_eval.json").write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", type=Path, default=Path("outputs/v2/windows_L1_toss-static_stand-exclude_walkonly.csv"))
    ap.add_argument("--report", type=Path, default=Path(
        "outputs/v2/toss-static_stand-exclude_walkonly/report_windows_L1_toss-static_stand-exclude_walkonly_date.json"))
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, default=Path("outputs/service_eval"))
    args = ap.parse_args()
    main(args.table, args.report, args.data, args.out)
