"""Run the night monitor (v2: lying incl. tossing vs walking model + service states).

Replay a recording (fast, or paced like live with --realtime):
    .venv\\Scripts\\python run_monitor.py --replay data\\raw_csi\\sujin\\sujin_slow_lie_down_01_csi_raw.csv
Live: pipe raw CSI rows (same columns as the raw CSV, header first) into stdin:
    <collector> | .venv\\Scripts\\python run_monitor.py --stdin
Install check (plan 5.1): save the motion baseline from a recording of quiet lying:
    .venv\\Scripts\\python run_monitor.py --replay quiet_lying.csv --save-baseline models\\baseline_room.json

Output: JSON lines on state changes and alerts (or every step with --every-step), then the
night summary. Each line describes moment `t`; it is printed about 1 s later (model look-ahead).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from motion_state.csi_io import N_SUB
from motion_state.monitor_v2 import NightMonitorV2, ServiceConfig
from motion_state.replay import iter_packets


def stdin_packets():
    reader = csv.reader(sys.stdin)
    header = next(reader)
    col = {name: i for i, name in enumerate(header)}
    sub = [col[f"sub_{i}"] for i in range(N_SUB)]
    for row in reader:
        if len(row) < len(header):
            continue
        try:
            t = pd.Timestamp(row[col["timestamp"]]).timestamp()
            amp = np.array([float(row[i]) for i in sub])
        except (ValueError, KeyError):
            continue
        yield t, row[col["rx"]].strip().upper(), amp


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--replay", type=Path)
    src.add_argument("--stdin", action="store_true")
    ap.add_argument("--model", type=Path, default=Path("models/lying_walking_v2.pkl"))
    ap.add_argument("--hold-sec", type=float, default=3.0, help="persistence rule A: seconds of activity")
    ap.add_argument("--baseline", type=Path, help="baseline JSON from --save-baseline")
    ap.add_argument("--save-baseline", type=Path, help="calibrate from this quiet-lying recording and save")
    ap.add_argument("--realtime", action="store_true", help="pace replay like a live stream")
    ap.add_argument("--every-step", action="store_true")
    args = ap.parse_args()

    cfg = ServiceConfig(hold_sec=args.hold_sec)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))["motion"] if args.baseline else None
    mon = NightMonitorV2(args.model, cfg, baseline)
    packets = iter_packets(args.replay) if args.replay else stdin_packets()
    last_key, wall0, t_first, last_t = None, time.monotonic(), None, 0.0

    def emit(outs):
        nonlocal last_key
        for o in outs:
            key = (o.state, o.internal)
            if args.save_baseline:
                continue
            if args.every_step or o.alert or key != last_key:
                print(json.dumps(o.to_dict(), ensure_ascii=False), flush=True)
                last_key = key

    for t, rx, a in packets:
        if args.realtime and args.replay:
            t_first = t if t_first is None else t_first
            delay = (t - t_first) - (time.monotonic() - wall0)
            if delay > 0:
                time.sleep(delay)
        mon.push_packet(t, rx, a)
        emit(mon.poll(t))
        last_t = t
    emit(mon.poll(last_t + 2.0))

    if args.save_baseline:
        if mon.baseline is None:
            sys.exit("not enough quiet data for a baseline (need about 20 s of lying still)")
        base = {"motion": mon.baseline, "source": str(args.replay)}
        args.save_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.save_baseline.write_text(json.dumps(base, indent=1), encoding="utf-8")
        print(json.dumps(base, ensure_ascii=False))
        return
    print(json.dumps({"night_summary": mon.summary()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
