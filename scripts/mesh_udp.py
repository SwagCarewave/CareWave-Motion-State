from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.pose.packets import FILE_HEADER, is_mesh_payload, iter_file


def _target(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host:
        host, port = port, "5005"
    return socket.gethostbyname(host), int(port)


def replay(args) -> None:
    dest = _target(args.to)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sent = 0
    with args.raw.open("rb") as f:
        origin = base = wall0 = None
        for stamp, payload in iter_file(f):
            if origin is None:
                origin = stamp
            if stamp - origin < args.skip:
                continue
            if base is None:
                base, wall0 = stamp, time.monotonic()
            rel = stamp - base
            if args.duration and rel > args.duration:
                break
            delay = wall0 + rel / args.speed - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            sock.sendto(payload, dest)
            sent += 1
            if sent % 5000 == 0:
                print(f"보냄 {sent}개 · {rel:.1f}초", flush=True)
    print(f"완료: {sent}개 패킷을 {dest[0]}:{dest[1]}로 보냄")


def relay(args) -> None:
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    try:
        rx.bind((args.host, args.port))
    except OSError:
        sys.exit(f"UDP {args.host}:{args.port} 포트를 열 수 없습니다. csi_v2_receiver.py 같은 다른 수신기를 끄거나 --port로 바꾸세요.")
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dests = [_target(d) for d in args.to]
    out = args.save.open("ab") if args.save else None
    count = other = 0
    last = time.monotonic()
    print(f"udp://{args.host}:{args.port} 수신 → {', '.join(f'{h}:{p}' for h, p in dests)}" +
          (f" · 저장 {args.save}" if out else ""), flush=True)
    try:
        while True:
            data, _ = rx.recvfrom(65535)
            for d in dests:
                tx.sendto(data, d)
            if is_mesh_payload(data):
                count += 1
                if out:
                    out.write(FILE_HEADER.pack(time.time(), len(data)) + data)
            else:
                other += 1
            if time.monotonic() - last >= 2.0:
                last = time.monotonic()
                print(f"중계 {count}개 (6대 형식) · 기타 {other}개", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        if out:
            out.close()
    print(f"종료: 중계 {count}개")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="ESP 6대 바이너리 CSI를 UDP로 재생하거나 중계합니다.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay", help="저장된 raw.bin을 실시간처럼 UDP로 보냅니다")
    r.add_argument("raw", type=Path)
    r.add_argument("--to", default="127.0.0.1:5005", help="받는 서버 HOST:PORT")
    r.add_argument("--speed", type=float, default=1.0)
    r.add_argument("--skip", type=float, default=0.0, help="앞부분 건너뛸 초")
    r.add_argument("--duration", type=float, default=0.0, help="보낼 길이(초). 0이면 끝까지")
    r.set_defaults(func=replay)
    y = sub.add_parser("relay", help="ESP가 PC로 보낸 데이터를 서버로 그대로 넘깁니다")
    y.add_argument("--host", default="0.0.0.0")
    y.add_argument("--port", type=int, default=5005, help="ESP가 보내는 PC 포트")
    y.add_argument("--to", nargs="+", default=["api.leewatertrue.site:5005"], help="넘길 곳 HOST:PORT (여러 개 가능)")
    y.add_argument("--save", type=Path, help="받은 데이터를 raw.bin 형식으로 이어서 저장")
    y.set_defaults(func=relay)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
