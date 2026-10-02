"""Plot the CSI motion signal over time for the lie-related recordings, with label spans."""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Patch

from motion_state.features import load_labels

F = "lag3_2s_med"
WALK_LEVEL = float(np.exp(-2.65))   # walking-level threshold used in the leave-one-date-out runs
SPANS = {   # label group -> (fill colour, legend text)
    "in_bed": ("#eb6834", "뒤척임·누운 채 움직임"),
    "move": ("#2a78d6", "눕기·일어나기 동작"),
    "standing": ("#8a8984", "서있기"),
}
GROUP = {"tossing": "in_bed", "arm_movement": "in_bed", "small_movement": "in_bed",
         "slow_lying_down": "move", "getting_up": "move", "transition": "move", "standing": "standing"}


def mmss(x, _):
    return f"{int(x // 60)}:{int(x % 60):02d}"


def main(cache: Path, labels_dir: Path, out: Path, samples: list[str]) -> None:
    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False
    df = pd.read_csv(cache / "windows.csv")
    still = float(np.exp(df[(df.label == "lying") & ~df.boundary][F].median()))
    fig, axes = plt.subplots(len(samples), 1, figsize=(13, 3.3 * len(samples)), sharey=True)
    for ax, sid in zip(np.atleast_1d(axes), samples):
        g = df[df.sample_id == sid]
        lab = load_labels(next(labels_dir.glob(f"*/{sid}_labels.csv")))
        for r in lab.itertuples():
            grp = GROUP.get(r.label)
            if grp:
                ax.axvspan(r.start_sec, r.end_sec, color=SPANS[grp][0], alpha=0.22, lw=0)
        ax.plot(g.t_center, np.exp(g[F]), color="#2b2a27", lw=1.6)
        ax.axhline(WALK_LEVEL, color="#e34948", ls="--", lw=1.2)
        ax.axhline(still, color="#52514e", ls=":", lw=1.2)
        ax.text(1.002, WALK_LEVEL, " 걷기 기준", transform=ax.get_yaxis_transform(), va="center",
                fontsize=9, color="#52514e")
        ax.text(1.002, still, " 가만히 누움\n (중앙값)", transform=ax.get_yaxis_transform(), va="center",
                fontsize=9, color="#52514e")
        ax.set_title(sid, loc="left", fontsize=11, color="#0b0b0b")
        ax.xaxis.set_major_formatter(plt.FuncFormatter(mmss))
        ax.set_xlim(0, g.t_center.max() + 0.5)
        ax.set_ylabel("CSI 흔들림 크기", color="#52514e")
        ax.grid(axis="y", color="#e6e5e0", lw=0.8)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    axes[-1].set_xlabel("영상 시간 (분:초)", color="#52514e")
    handles = [Patch(color=c, alpha=0.35, label=t) for c, t in SPANS.values()]
    handles.append(Patch(color="white", label="(배경 없음) 가만히 누움"))
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.01, 0.965), ncol=4, frameon=False, fontsize=10)
    fig.suptitle("누워있기 관련 녹화의 CSI 흔들림 (0.3초 전 대비 CSI 모양 변화, 2초 평균)", x=0.01, ha="left",
                 fontsize=13)
    fig.tight_layout(rect=(0, 0, 0.95, 0.94))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130)
    print(f"saved {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=Path("outputs/cache"))
    ap.add_argument("--labels", type=Path, default=Path("data/labels"))
    ap.add_argument("--out", type=Path, default=Path("outputs/figures/lie_csi_motion.png"))
    ap.add_argument("--samples", nargs="+", default=[
        "sujin_lie_down_normal_01", "hoyeon_lie_down_normal_01", "sujin_slow_lie_down_01"])
    args = ap.parse_args()
    main(args.cache, args.labels, args.out, args.samples)
