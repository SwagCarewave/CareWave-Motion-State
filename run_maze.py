"""Maze game demo: print the direction (상/우/하/좌) for every 0.5 s score.

Replay a recording (add --realtime to pace it like live input):
    .venv\\Scripts\\python run_maze.py --replay data\\raw_csi\\test\\yena_test_02_csi_raw.csv --realtime
Live: pipe raw CSI rows (same columns as the raw CSV, header first) into stdin:
    <collector> | .venv\\Scripts\\python run_maze.py --stdin
Each line describes moment t and is printed about 1 s later (model look-ahead).
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from motion_state.maze import direction
from motion_state.monitor_v2 import NightMonitorV2
from motion_state.replay import iter_packets
from run_monitor import stdin_packets


def show(o) -> None:
    if not o.signal["ok"]:
        text = "신호 확인 필요"
    elif o.activity_score is None:
        text = "준비 중"
    else:
        text = direction(o.activity_score)[1]
    score = "  -  " if o.activity_score is None else f"{o.activity_score:.2f}"
    print(f"{o.t:7.1f}s  점수 {score}  →  {text}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--replay", type=Path)
    src.add_argument("--stdin", action="store_true")
    ap.add_argument("--realtime", action="store_true", help="pace a replay like live input")
    ap.add_argument("--model", type=Path, default=Path("models/lying_walking_v2.pkl"))
    args = ap.parse_args()
    mon = NightMonitorV2(args.model)
    packets = stdin_packets() if args.stdin else iter_packets(args.replay)
    start, last_t = time.monotonic(), 0.0
    for t, rx, a in packets:
        if args.replay and args.realtime:
            delay = t - (time.monotonic() - start)
            if delay > 0:
                time.sleep(delay)
        mon.push_packet(t, rx, a)
        for o in mon.poll(t):
            show(o)
        last_t = t
    for o in mon.poll(last_t + 2.0):
        show(o)


if __name__ == "__main__":
    main()
