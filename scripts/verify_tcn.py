from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.pose.packets import PacketError, iter_file, parse_payload
from app.pose.registry import load_runtime


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="13관절 TCN 어댑터가 Skeleton-Model의 StreamingPipeline과 같은 값을 내는지 비교합니다.")
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--model", type=Path, required=True, help="models/pose/<이름> 폴더")
    ap.add_argument("--reference", type=Path, required=True, help="Skeleton-Model 저장소 폴더")
    ap.add_argument("--seconds", type=float, default=120.0)
    args = ap.parse_args()

    sys.path.insert(0, str(args.reference))
    from csipose.model import load_checkpoint
    from csipose.realtime import StreamingPipeline

    ckpt = sorted(args.model.glob("*.pt"))[0]
    ref_model, _ = load_checkpoint(ckpt)
    ref = StreamingPipeline(ref_model)
    mine = load_runtime(args.model)
    mine.infer_last = 1

    ref_out, mine_out, bad = [], [], 0
    first = None
    with args.raw.open("rb") as f:
        for stamp, payload in iter_file(f):
            first = stamp if first is None else first
            if stamp - first > args.seconds:
                break
            ref.push(stamp, payload)
            try:
                _, records = parse_payload(payload)
            except PacketError:
                bad += 1
                records = []
            mine_out.extend(mine.poll(stamp))
            for r in records:
                mine.add(stamp, r)
            ref_out.extend(ref.drain(stamp))

    print(f"reference outputs {len(ref_out)}, adapter outputs {len(mine_out)}, skipped packets {bad}")
    n = min(len(ref_out), len(mine_out))
    if not n:
        sys.exit("비교할 출력이 없습니다 (보정 10초보다 긴 구간이 필요합니다)")
    dt = max(abs(r.t - m["ts"]) for r, m in zip(ref_out[:n], mine_out[:n]))
    size = np.array([m["pixels"]["width"], m["pixels"]["height"]], np.float32) if (m := mine_out[0]) else 1
    kp_ref = np.stack([r.keypoints for r in ref_out[:n]])
    kp_mine = np.stack([[[p["x"], p["y"]] for p in m["smoothed"]] for m in mine_out[:n]]) * size
    pr_ref = np.stack([r.probs for r in ref_out[:n]])
    pr_mine = np.stack([[m["probabilities"][c] for c in mine.manifest["classes"]] for m in mine_out[:n]])
    act = np.mean([r.activity == m["action"]["code"] for r, m in zip(ref_out[:n], mine_out[:n])])
    fall = np.mean([r.fall_alarm == m["fall_alarm"] for r, m in zip(ref_out[:n], mine_out[:n])])
    print(f"compared {n}, max time diff {dt:.4f} s")
    print(f"keypoints px abs diff: max {np.abs(kp_ref - kp_mine).max():.3f}, mean {np.abs(kp_ref - kp_mine).mean():.4f} (출력 반올림 0.0001 ≈ 0.2px)")
    print(f"prob abs diff: max {np.abs(pr_ref - pr_mine).max():.2e}")
    print(f"activity agreement {act * 100:.2f}%, fall alarm agreement {fall * 100:.2f}%")


if __name__ == "__main__":
    main()
