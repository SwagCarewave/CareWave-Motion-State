from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import deque
from pathlib import Path
from typing import Iterator

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.timeutil import parse_ts
from motion_state.csi_io import N_SUB

SUB = [f"sub_{i}" for i in range(N_SUB)]


def read_rows(source) -> Iterator[dict]:
    reader = csv.reader(source)
    header = next(reader)
    col = {name.strip(): i for i, name in enumerate(header)}
    missing = [c for c in ("timestamp", "rx", *SUB) if c not in col]
    if missing:
        sys.exit(f"CSV 헤더에 필수 열이 없습니다: {', '.join(missing[:5])}")
    idx = [col[c] for c in SUB]
    for row in reader:
        if len(row) <= max(idx[0], col["rx"], col["timestamp"]):
            continue
        try:
            ts = parse_ts(row[col["timestamp"]])
        except ValueError:
            continue
        amp = [float(row[i]) if i < len(row) and row[i].strip() else None for i in idx]
        yield {"ts": ts, "rx": row[col["rx"]].strip().upper(), "amplitude": amp}


class Bridge:
    def __init__(self, base_url: str, session_id: str | None, batch: int, interval: float, buffer_max: int):
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(15.0, connect=5.0))
        self.session_id = session_id
        self.batch = batch
        self.interval = interval
        self.buffer: deque = deque(maxlen=buffer_max)
        self.last_send = 0.0
        self.dropped = 0
        self.stats = {"accepted": 0, "rejected_invalid": 0, "rejected_incomplete": 0, "rejected_stale": 0, "frames": 0}

    def ensure_session(self) -> str:
        if self.session_id is None:
            res = self.client.post("/api/sessions")
            res.raise_for_status()
            self.session_id = res.json()["id"]
            print(f"세션: {self.session_id} ({'새로 시작' if res.json()['created'] else '실행 중인 세션 사용'})")
        return self.session_id

    def add(self, packet: dict) -> None:
        if len(self.buffer) == self.buffer.maxlen:
            self.dropped += 1
        self.buffer.append(packet)
        if len(self.buffer) >= self.batch or time.monotonic() - self.last_send >= self.interval:
            self.send()

    def send(self) -> bool:
        if not self.buffer:
            return True
        packets = list(self.buffer)[: self.batch]
        try:
            sid = self.ensure_session()
            res = self.client.post(f"/api/sessions/{sid}/packets", json={"packets": packets})
        except httpx.HTTPError as exc:
            print(f"전송 실패, 버퍼 {len(self.buffer)}개 보관: {exc}", file=sys.stderr)
            self.last_send = time.monotonic()
            return False
        self.last_send = time.monotonic()
        if res.status_code == 409:
            print("세션이 종료되어 새 세션을 시작합니다.", file=sys.stderr)
            self.session_id = None
            return False
        if res.status_code >= 500 or res.status_code == 429:
            print(f"서버 오류 {res.status_code}, 버퍼 {len(self.buffer)}개 보관", file=sys.stderr)
            return False
        if res.status_code >= 400:
            print(f"요청 거부 {res.status_code}: {res.text[:200]}", file=sys.stderr)
            for _ in packets:
                self.buffer.popleft()
            return False
        for k, v in res.json().items():
            self.stats[k] += v
        for _ in packets:
            self.buffer.popleft()
        return True

    def drain(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        backoff = 0.5
        while self.buffer and time.monotonic() < end:
            if not self.send():
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
            else:
                backoff = 0.5


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="CSI 수집기 출력(CSV 형식)을 CareWave API로 묶어서 보냅니다.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--stdin", action="store_true", help="표준입력으로 CSV 줄을 받습니다 (수집기 | python collector_bridge.py --stdin)")
    src.add_argument("--replay", type=Path, help="저장된 CSV를 실시간처럼 재생해서 보냅니다 (데모·점검용)")
    ap.add_argument("--api", default="http://127.0.0.1:8000", help="API 주소")
    ap.add_argument("--session", help="보낼 세션 ID. 없으면 시작하거나 실행 중인 세션을 사용")
    ap.add_argument("--batch", type=int, default=200, help="한 번에 보낼 최대 패킷 수")
    ap.add_argument("--interval", type=float, default=0.5, help="최대 전송 간격(초)")
    ap.add_argument("--buffer", type=int, default=50000, help="서버가 응답하지 않을 때 보관할 최대 패킷 수")
    ap.add_argument("--speed", type=float, default=1.0, help="--replay 재생 배속")
    ap.add_argument("--keep-time", action="store_true", help="--replay 때 원래 시각을 그대로 보냄 (기본: 시작 시각만 지금으로 옮기고 간격은 유지)")
    args = ap.parse_args()

    bridge = Bridge(args.api, args.session, args.batch, args.interval, args.buffer)
    source = sys.stdin if args.stdin else args.replay.open(encoding="utf-8-sig", newline="")
    first_ts = wall0 = None
    count = 0
    try:
        for packet in read_rows(source):
            if args.replay:
                if first_ts is None:
                    first_ts, wall0 = packet["ts"], time.time()
                rel = (packet["ts"] - first_ts) / args.speed
                delay = wall0 + rel - time.time()
                if delay > 0:
                    time.sleep(delay)
                if not args.keep_time:
                    packet["ts"] = wall0 + (packet["ts"] - first_ts)
            bridge.add(packet)
            count += 1
            if count % 2000 == 0:
                print(f"읽음 {count} · 보냄 {bridge.stats['accepted']} · 버퍼 {len(bridge.buffer)}")
    except KeyboardInterrupt:
        print("중단 요청, 남은 패킷을 보냅니다.")
    finally:
        bridge.drain(timeout=30.0)
        if source is not sys.stdin:
            source.close()
    print(f"완료: 읽음 {count}, 결과 {bridge.stats}, 버퍼 초과로 버림 {bridge.dropped}, 미전송 {len(bridge.buffer)}")


if __name__ == "__main__":
    main()
