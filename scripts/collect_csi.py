from __future__ import annotations

import argparse
import csv
import socket
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.esp_csi import CsiParseError, parse_line
from motion_state.csi_io import SUB_COLS

KST = timezone(timedelta(hours=9))
HEADER = ["experiment_id", "timestamp", "label", "rx", *SUB_COLS]


def udp_lines(host: str, port: int, stop_at: float | None) -> Iterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
    except OSError:
        sys.exit(f"UDP {host}:{port} 포트를 열 수 없습니다. 서버가 같은 포트로 수신 중이면 서버를 끄거나 "
                 f"CAREWAVE_UDP_ENABLED=false 로 실행하거나 --port 로 다른 포트를 쓰세요.")
    sock.settimeout(0.5)
    try:
        while stop_at is None or time.monotonic() < stop_at:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                yield ""
                continue
            yield from data.decode("utf-8", errors="ignore").splitlines()
    finally:
        sock.close()


def stdin_lines(stop_at: float | None) -> Iterator[str]:
    for line in sys.stdin:
        if stop_at is not None and time.monotonic() >= stop_at:
            return
        yield line


def default_out(label: str) -> Path:
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    return ROOT / "outputs" / "collected" / f"csi_raw_{label}_{stamp}.csv"


def collect(lines: Iterator[str], out: Path, label: str, experiment_id: str, report_sec: float = 2.0) -> Counter:
    out.parent.mkdir(parents=True, exist_ok=True)
    stats: Counter = Counter()
    last_ts: dict[str, datetime] = {}
    last_report = last_flush = time.monotonic()
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        try:
            for line in lines:
                now = time.monotonic()
                if line.strip():
                    _write(writer, line, label, experiment_id, last_ts, stats)
                if now - last_flush >= 1.0:
                    handle.flush()
                    last_flush = now
                if now - last_report >= report_sec:
                    last_report = now
                    print(_summary(stats), flush=True)
        except KeyboardInterrupt:
            pass
    return stats


def _write(writer, line: str, label: str, experiment_id: str, last_ts: dict, stats: Counter) -> None:
    stats["lines"] += 1
    try:
        packet = parse_line(line)
    except CsiParseError as exc:
        stats[f"rejected_{exc.reason}"] += 1
        return
    ts = datetime.now(KST)
    prev = last_ts.get(packet.rx)
    if prev is not None and ts <= prev:
        ts = prev + timedelta(microseconds=1)
    last_ts[packet.rx] = ts
    writer.writerow([experiment_id, ts.isoformat(), label, packet.rx, *(round(float(a), 6) for a in packet.amplitude)])
    stats[packet.rx] += 1
    stats["saved"] += 1


def _summary(stats: Counter) -> str:
    per_rx = " ".join(f"{rx}={stats[rx]}" for rx in ("RX1", "RX2", "RX3"))
    rejected = {k[9:]: v for k, v in stats.items() if k.startswith("rejected_")}
    return f"저장 {stats['saved']} ({per_rx}) · 거부 {sum(rejected.values())} {rejected or ''}".rstrip()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="ESP32 CSI(raw)를 받아 학습 데이터와 같은 형식의 CSV로 저장합니다. Ctrl+C로 끝냅니다.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--udp", action="store_true", help="UDP로 받기 (기본)")
    src.add_argument("--stdin", action="store_true", help="표준입력으로 받기 (시리얼 모니터 출력을 파이프로 연결)")
    ap.add_argument("--host", default="0.0.0.0", help="UDP 수신 주소")
    ap.add_argument("--port", type=int, default=5005, help="UDP 수신 포트")
    ap.add_argument("--label", default="unlabeled", help="CSV label 열 값 (예: lie_down, walk)")
    ap.add_argument("--out", type=Path, help="저장 경로 (기본: outputs/collected/csi_raw_<label>_<시각>.csv)")
    ap.add_argument("--duration", type=float, help="이 시간(초)이 지나면 자동 종료")
    args = ap.parse_args()

    out = args.out or default_out(args.label)
    experiment_id = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    stop_at = None if args.duration is None else time.monotonic() + args.duration
    lines = stdin_lines(stop_at) if args.stdin else udp_lines(args.host, args.port, stop_at)
    source = "표준입력" if args.stdin else f"udp://{args.host}:{args.port}"
    print(f"수집 시작: {source} → {out} (label={args.label}, experiment_id={experiment_id})", flush=True)
    stats = collect(lines, out, args.label, experiment_id)
    print(f"수집 종료: {_summary(stats)} → {out}", flush=True)


if __name__ == "__main__":
    main()
