from __future__ import annotations

from collections import deque
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from app.pose.output import OutputShaper
from app.pose.packets import LinkRecord
from app.pose.registry import PoseModelError, check_file

N_NODES = 6
HT_INDICES = np.r_[65:93, 100:128]
TCN_LINKS = [(tx, rx) for tx in range(1, N_NODES + 1) for rx in range(1, N_NODES + 1) if tx != rx]
LINK_INDEX = {pair: i for i, pair in enumerate(TCN_LINKS)}
SHOULDERS, HIPS = [1, 2], [7, 8]


class CausalBlock(nn.Module):
    def __init__(self, ch: int, dilation: int, dropout: float):
        super().__init__()
        self.pad = 2 * dilation
        self.conv1 = nn.Conv1d(ch, ch, 3, dilation=dilation)
        self.conv2 = nn.Conv1d(ch, ch, 1)
        self.norm = nn.GroupNorm(8, ch)
        self.drop = nn.Dropout(dropout)

    def forward(self, x):
        y = self.conv1(F.pad(x, (self.pad, 0)))
        y = self.drop(F.gelu(self.norm(y)))
        return x + self.conv2(y)


def _causal_mean(x, frames: int):
    return F.avg_pool1d(F.pad(x[:, None], (frames - 1, 0), mode="replicate"), frames, stride=1)[:, 0]


def _frame_diff(x):
    return torch.cat([torch.zeros_like(x[:, :1]), x[:, 1:] - x[:, :-1]], dim=1)


class CsiPoseNet(nn.Module):
    def __init__(self, n_links: int, n_features: int, n_joints: int, n_classes: int, hidden: int = 128,
                 link_dim: int = 12, dropout: float = 0.15, motion_input: bool = False, posture_head: bool = False,
                 smooth_frames: int = 10, n_posture: int = 11):
        super().__init__()
        self.motion_input, self.posture_head = motion_input, posture_head
        self.n_joints, self.smooth_frames = n_joints, smooth_frames
        link_in = n_features * (2 if motion_input else 1)
        self.link_mlp = nn.Sequential(nn.Linear(link_in, 24), nn.GELU(), nn.Linear(24, link_dim))
        self.link_bias = nn.Parameter(torch.zeros(n_links, link_dim))
        self.proj = nn.Sequential(nn.Dropout(dropout), nn.Linear(n_links * link_dim, hidden), nn.GELU())
        self.tcn = nn.Sequential(*[CausalBlock(hidden, d, dropout) for d in (1, 2, 4, 8, 16)])
        self.pose = nn.Sequential(nn.Linear(hidden, hidden), nn.GELU(), nn.Linear(hidden, n_joints * 2))
        act_in = hidden + (n_posture if posture_head else 0)
        self.activity = nn.Sequential(nn.Linear(act_in, 64), nn.GELU(), nn.Linear(64, n_classes))
        self.register_buffer("feat_mean", torch.zeros(n_links, n_features))
        self.register_buffer("feat_std", torch.ones(n_links, n_features))
        self.register_buffer("pose_mean", torch.zeros(n_joints, 2))
        self.register_buffer("pose_std", torch.ones(1))

    def normalize(self, x):
        return (x - self.feat_mean) / self.feat_std

    def posture_features(self, pose, x):
        raw = pose.detach()
        norm = (raw - self.pose_mean) / self.pose_std
        torso = raw[:, :, HIPS].mean(2) - raw[:, :, SHOULDERS].mean(2)
        length = torso.norm(dim=-1, keepdim=True)
        direction = torso / (length + 1e-3)
        speed = _frame_diff(raw).norm(dim=-1).mean(-1) / self.pose_std
        motion = _frame_diff(x).abs().mean((2, 3))
        return torch.cat([
            direction, length / self.pose_std, norm.mean(2), norm.amax(2) - norm.amin(2),
            speed[..., None], _causal_mean(speed, self.smooth_frames)[..., None],
            motion[..., None], _causal_mean(motion, self.smooth_frames)[..., None],
        ], dim=-1)

    def forward(self, x):
        b, t = x.shape[:2]
        inp = torch.cat([x, _frame_diff(x).abs()], dim=-1) if self.motion_input else x
        h = self.link_mlp(inp) + self.link_bias
        h = self.proj(h.reshape(b, t, -1)).transpose(1, 2)
        h = self.tcn(h).transpose(1, 2)
        pose = self.pose(h).reshape(b, t, self.n_joints, 2) * self.pose_std + self.pose_mean
        act_in = torch.cat([h, self.posture_features(pose, x)], dim=-1) if self.posture_head else h
        return pose, self.activity(act_in)


def entry_feature(entry: bytes, group: int) -> np.ndarray:
    iq = np.frombuffer(entry[96:352], dtype=np.int8).astype(np.float32).reshape(128, 2)
    csi = iq[HT_INDICES, 1] + 1j * iq[HT_INDICES, 0]
    amp = np.abs(csi.astype(np.complex64))
    amp = amp / max(float(amp.mean()), 1e-3)
    n_groups = len(HT_INDICES) // group
    grouped = amp[:n_groups * group].reshape(n_groups, group).mean(axis=1)
    rssi = float(entry[26] - 256 if entry[26] > 127 else entry[26])
    return np.concatenate([grouped, [rssi / 50.0 + 1.0]]).astype(np.float32)


class TcnRuntime:
    def __init__(self, manifest: dict, net: CsiPoseNet):
        cfg = manifest["cfg"]
        self.manifest, self.net = manifest, net
        self.name = manifest["name"]
        self.links = manifest["links"]
        self.hz = float(cfg["feature_hz"])
        self.dt = 1.0 / self.hz
        self.window = int(cfg["window"])
        self.calib_frames = int(round(cfg["calib_sec"] * self.hz))
        self.late = float(cfg.get("late_sec", 0.03))
        self.ema_alpha = float(cfg.get("smooth", 0.35))
        self.group = int(cfg.get("subcarrier_group", 4))
        self.good_kind = int(cfg.get("good_kind", 6))
        self.infer_last = int(cfg.get("infer_last_bins", 2))
        self.size = np.array(manifest["image_size"], np.float32)
        self.fall_threshold = float(manifest.get("fall", {}).get("threshold", 0.7))
        self.fall_bins = int(round(manifest.get("fall", {}).get("hold_sec", 0.4) * self.hz))
        self.fall_index = manifest["classes"].index("fall") if "fall" in manifest["classes"] else None
        self.shaper = OutputShaper(self.name, manifest["points"], manifest["classes"], manifest.get("display", {}),
                                   manifest.get("game", {}), {})
        self.reset()

    def reset(self) -> None:
        self.t0: float | None = None
        self.next_bin = 0
        self.pending: list[tuple[float, int, np.ndarray]] = []
        self.last = self.net.feat_mean.numpy().astype(np.float32).copy()
        self.calib: list[np.ndarray] = []
        self.offset: np.ndarray | None = None
        self.history: deque = deque(maxlen=self.window)
        self.ema: np.ndarray | None = None
        self.fall_run = 0
        self.bins_since_infer = 0
        self.bins_total = 0
        self.bins_full = 0
        self.predictions = 0
        self.links_seen = 0
        self.shaper.reset()

    def calibration(self) -> dict:
        return {"required": True, "active": self.offset is None,
                "progress": round(min(1.0, len(self.calib) / self.calib_frames), 3),
                "seconds": round(self.calib_frames / self.hz, 1)}

    def quality(self) -> dict:
        return {"valid_frame_ratio": round(self.bins_full / self.bins_total, 3) if self.bins_total else None,
                "links_seen": self.links_seen}

    def add(self, ts: float, record: LinkRecord) -> bool:
        tx, rx = record.peer, record.owner
        if self.t0 is None:
            self.t0 = ts
        li = LINK_INDEX.get((tx, rx))
        if li is None:
            return False
        if record.entry[25] == self.good_kind:
            self.pending.append((ts, li, entry_feature(record.entry, self.group)))
        return True

    def poll(self, upto: float, behind: bool = False, max_lag_bins: int = 200) -> list[dict]:
        if self.t0 is None:
            return []
        target = int(np.floor((upto - self.late - self.t0) * self.hz))
        if target - self.next_bin > max_lag_bins:
            self.next_bin = target - max_lag_bins
            self.pending = [p for p in self.pending if p[0] >= self.t0 + self.next_bin * self.dt]
        out = []
        while self.next_bin < target:
            infer = not behind and self.next_bin >= target - self.infer_last
            frame = self._step(self.next_bin, infer)
            if frame is not None:
                out.append(frame)
            self.next_bin += 1
        return out

    def _step(self, b: int, infer: bool) -> dict | None:
        start = self.t0 + b * self.dt
        end = start + self.dt
        now = [p for p in self.pending if p[0] < end]
        self.pending = [p for p in self.pending if p[0] >= end]
        n = len(self.links)
        total = np.zeros((n, self.last.shape[1]), np.float32)
        count = np.zeros(n, np.int32)
        for ts, li, feat in now:
            if int(np.floor((ts - start) * self.hz)) == 0:
                total[li] += feat
                count[li] += 1
        observed = count > 0
        self.last = np.where(observed[:, None], total / np.maximum(count, 1)[:, None], self.last).astype(np.float32)
        self.links_seen = int(observed.sum())
        self.bins_total += 1
        self.bins_full += int(observed.all())
        self.history.append(self.last.copy())
        if self.offset is None:
            self.calib.append(self.last.copy())
            if len(self.calib) >= self.calib_frames:
                self.offset = np.mean(self.calib, axis=0)
            return None
        self.bins_since_infer += 1
        if not infer:
            return None
        window = np.stack(self.history)
        with torch.inference_mode():
            x = self.net.normalize(torch.from_numpy(window - self.offset))[None]
            pose, logits = self.net(x)
        kp = pose[0, -1].numpy()
        probs = logits[0, -1].softmax(-1).numpy()
        self.ema = kp if self.ema is None else self.ema_alpha * kp + (1 - self.ema_alpha) * self.ema
        if self.fall_index is not None and probs[self.fall_index] > self.fall_threshold:
            self.fall_run += self.bins_since_infer
        else:
            self.fall_run = 0
        self.bins_since_infer = 0
        self.predictions += 1
        frame = self.shaper.frame(end, kp / self.size, self.ema / self.size, probs)
        frame["fall_alarm"] = self.fall_run >= self.fall_bins
        frame["links_seen"] = self.links_seen
        frame["pixels"] = {"width": int(self.size[0]), "height": int(self.size[1])}
        return frame


def load(directory: Path, manifest: dict, verify: bool = True) -> TcnRuntime:
    path = check_file(directory, manifest["weights"][0], verify)
    ckpt = torch.load(path, map_location="cpu", weights_only=True)
    args = ckpt.get("args", {})
    cfg = manifest["cfg"]
    net = CsiPoseNet(len(manifest["links"]), int(cfg["n_features"]), len(manifest["points"]), len(manifest["classes"]),
                     hidden=args.get("hidden", 128), dropout=args.get("dropout", 0.15),
                     motion_input=args.get("motion_input", False), posture_head=args.get("posture_head", False),
                     smooth_frames=int(cfg.get("smooth_frames", 10)))
    try:
        net.load_state_dict(ckpt["state"])
    except RuntimeError as exc:
        raise PoseModelError(f"weights do not match the network: {exc}") from exc
    net.eval()
    return TcnRuntime(manifest, net)
