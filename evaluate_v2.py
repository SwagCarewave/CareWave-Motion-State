"""Final holdout evaluation: lying (still + tossing) vs walking model and the service alerts.

Run once on the private holdout: any folder containing <id>_csi_raw.csv and <id>_labels.csv
files (subfolder names do not matter). Settings are frozen; do not tune anything after seeing
the result.
    .venv\\Scripts\\python evaluate_v2.py --data C:\\path\\to\\holdout

Every recording is replayed packet by packet through the streaming detector and NightMonitorV2,
exactly as in live use. Two results are printed:
  1. 0.5 s decisions: lying (still + tossing) vs walking; other actions and standing excluded
  2. alerts (plan 11): walks alerted, latency, false alerts while lying or tossing
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from build_v2 import change_distance, label_class
from eval_service import evaluate as evaluate_alerts
from eval_service import group, stream_decisions
from motion_state.features import frame_labels, load_labels
from motion_state.monitor_v2 import NightMonitorV2, ServiceConfig
from train_v2 import report

OK = {"ok": True, "packet_rate": {}, "reason": None}


def main(data: Path, model: Path, out: Path, hold: float) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    out.mkdir(parents=True, exist_ok=True)
    win_parts, mon_parts = [], []
    # any folder layout: files are matched by name (<id>_csi_raw.csv <-> <id>_labels.csv)
    raws = sorted(data.rglob("*_csi_raw.csv"))
    for i, p in enumerate(raws, 1):
        sid = p.name.removesuffix("_csi_raw.csv")
        lab_files = list(data.rglob(f"{sid}_labels.csv"))
        if not lab_files:
            print(f"skip {sid}: no label file")
            continue
        labels = load_labels(lab_files[0])
        decs = stream_decisions(p, model)
        # 1) decision level
        d = pd.DataFrame([x.__dict__ for x in decs])
        d = d[d.state != "warming_up"].reset_index(drop=True)
        lab = frame_labels(d.t.to_numpy(), labels)
        cls = np.array([label_class(x, "static", "exclude", True) for x in lab], dtype=object)
        win_parts.append(d.assign(sample_id=sid, label=lab, cls=cls, dchg=change_distance(d.t.to_numpy(), cls),
                                  p=d.probability, pred=np.where(d.state == "dynamic", "dynamic", "static"),
                                  fold="holdout"))
        # 2) service level
        mon = NightMonitorV2(model, ServiceConfig(hold_sec=hold))
        r = pd.DataFrame([mon.on_decision(x, OK).to_dict() for x in decs])
        mlab = frame_labels(r.t.to_numpy(), labels)
        mon_parts.append(r.assign(sample_id=sid, label=mlab, g=[group(x) for x in mlab]))
        print(f"[{i}/{len(raws)}] {sid}", flush=True)

    win = pd.concat(win_parts, ignore_index=True)
    rep = report(win, n_boot=500)
    alerts = evaluate_alerts(pd.concat(mon_parts, ignore_index=True), 1.0, hold)
    (out / "holdout_report.json").write_text(json.dumps({"decisions": rep, "alerts": alerts}, indent=1,
                                                        ensure_ascii=False), encoding="utf-8")
    win.to_csv(out / "holdout_decisions.csv", index=False)

    o = rep["overall"]
    L = rep["by_label_correct"]
    pick = lambda k: f"{L[k]['correct']:.1%} ({L[k]['sec']:.0f}초)" if k in L else "데이터 없음"
    print("\n=== 1. 0.5초 판정: 눕기(가만히 누움 + 뒤척임) vs 걷기 ===")
    print(f"정확도 {o['accuracy']:.1%} (95% 신뢰구간 {o['accuracy_95ci'][0]:.1%}~{o['accuracy_95ci'][1]:.1%}), "
          f"채점 시간 {o['seconds']:.0f}초")
    print(f"걷기 → 걷기 수준: {pick('walking')}")
    print(f"가만히 누움 → 걷기 아님: {pick('lying')}")
    print(f"뒤척임 → 걷기 아님: {pick('tossing(in-bed)')}")
    a = alerts
    print(f"\n=== 2. 알림 ({hold:g}초 지속 규칙) ===")
    print(f"준비 후 시작한 걷기 알림: {a['alerted_after_warmup']}/{a['walks_after_warmup']}, "
          f"지연 중앙값 {a['latency_after_warmup_sec']['median']}초 / 상위 5% {a['latency_after_warmup_sec']['p95']}초")
    print(f"녹화 시작부터 걷던 경우 알림: {a['alerted_from_warmup']}/{a['walks_from_warmup']} "
          f"(준비 시간 때문에 평가 못 한 짧은 걷기 {a['too_short_after_warmup']}개)")
    print(f"누움·뒤척임 중 오알림: {a['false_alerts']}회 / 누워있던 시간 {a['lying_minutes']}분")
    print(f"뒤척임 구간에서 알림: {a['tossing_segments_alerted']}/{a['tossing_segments']}")
    print(f"서있기 중 알림 {a['standing_alerts']}회, 다른 실제 활동 중 알림 {a['activity_alerts']}회")
    print(f"\n결과 파일: {out / 'holdout_report.json'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--model", type=Path, default=Path("models/lying_walking_v2.pkl"))
    ap.add_argument("--hold-sec", type=float, default=3.0)
    ap.add_argument("--out", type=Path, default=Path("outputs/holdout_eval"))
    args = ap.parse_args()
    main(args.data, args.model, args.out, args.hold_sec)
