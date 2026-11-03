from __future__ import annotations

import math
from collections import deque
from pathlib import Path

import numpy as np
import torch
from torch import nn

from app.pose.features import FeatureBuilder
from app.pose.output import OutputShaper
from app.pose.packets import N_BINS, LinkRecord
from app.pose.registry import PoseModelError, check_file


class UpperNet(nn.Module):
    def __init__(self, n_links: int, n_classes: int, n_points: int, n_bins: int = N_BINS):
        super().__init__()
        self.links, self.bins, self.points = n_links, n_bins, n_points
        self.freq = nn.Sequential(nn.Conv1d(2, 16, 5, padding=2, stride=2), nn.GELU(),
                                  nn.Conv1d(16, 32, 3, padding=1, stride=2), nn.GELU(), nn.AdaptiveAvgPool1d(1))
        self.fuse = nn.Sequential(nn.Linear(n_links * 33, 128), nn.LayerNorm(128), nn.GELU(), nn.Dropout(0.2))
        self.gru = nn.GRU(128, 128, batch_first=True)
        self.xy = nn.Linear(128, n_points * 2)
        self.action = nn.Linear(128, n_classes)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        b, t, features = x.shape
        n = self.links * self.bins
        if features != 2 * n + self.links:
            raise ValueError("feature count mismatch")
        a = x[:, :, :n].reshape(b, t, self.links, self.bins)
        d = x[:, :, n:2 * n].reshape(b, t, self.links, self.bins)
        z = self.freq(torch.stack([a, d], dim=3).reshape(b * t * self.links, 2, self.bins)).reshape(b, t, self.links * 32)
        z = self.fuse(torch.cat([z, x[:, :, 2 * n:]], -1))
        z = self.gru(z)[0][:, -1]
        return self.xy(z).reshape(b, self.points, 2), self.action(z)


class UpperModel:
    def __init__(self, manifest: dict, median: np.ndarray, iqr: np.ndarray, nets: list[UpperNet]):
        cfg = manifest["cfg"]
        self.manifest = manifest
        self.name = manifest["name"]
        self.links, self.points, self.classes = manifest["links"], manifest["points"], manifest["classes"]
        self.hz, self.window = float(cfg["hz"]), int(cfg["window"])
        self.stride, self.max_age = int(cfg["stride"]), float(cfg["max_age"])
        self.median, self.iqr, self.nets = median, iqr, nets

    @property
    def n_features(self) -> int:
        return 2 * len(self.links) * N_BINS + len(self.links)

    @torch.no_grad()
    def predict(self, windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        x = torch.from_numpy(((windows - self.median) / self.iqr).astype(np.float32))
        xy, prob = [], []
        for net in self.nets:
            p, logits = net(x)
            xy.append(p.numpy())
            prob.append(logits.softmax(-1).numpy())
        return np.mean(xy, axis=0), np.mean(prob, axis=0)


class UpperRuntime:
    def __init__(self, model: UpperModel):
        m = model.manifest
        self.model = model
        self.manifest = m
        self.name = model.name
        self.links = model.links
        self.step = 1.0 / model.hz
        self.shaper = OutputShaper(model.name, model.points, model.classes, m.get("display", {}), m.get("game", {}),
                                   m.get("fall", {}))
        self.tau = m.get("display", {}).get("smooth_tau_sec", 0.15)
        self.reset()

    def reset(self) -> None:
        self.builder = FeatureBuilder(self.model.links, self.model.max_age)
        self.frames: deque = deque(maxlen=self.model.window)
        self.next_target: float | None = None
        self.counter = 0
        self.last_pred_t: float | None = None
        self.smoothed: np.ndarray | None = None
        self.shaper.reset()
        self.frames_total = 0
        self.frames_valid = 0
        self.predictions = 0

    def calibration(self) -> dict | None:
        return None

    def quality(self) -> dict:
        total = self.frames_total
        return {"valid_frame_ratio": round(self.frames_valid / total, 3) if total else None}

    def add(self, ts: float, record: LinkRecord) -> bool:
        return self.add_amplitude(ts, record.link, record.amplitude)

    def add_amplitude(self, ts: float, link: str, amplitude: np.ndarray) -> bool:
        if self.next_target is None:
            self.next_target = math.floor(ts * self.model.hz + 1) / self.model.hz
        return self.builder.add(ts, link, amplitude)

    def poll(self, upto: float, max_lag: float = 2.0) -> list[dict]:
        if self.next_target is None:
            return []
        if upto - self.next_target > max_lag:
            self.next_target = math.floor((upto - self.step) * self.model.hz) / self.model.hz
            self.builder.reset()
            self._break()
        out = []
        while self.next_target <= upto:
            target = self.next_target
            self.next_target = round(target + self.step, 6)
            x, _ = self.builder.frame(target)
            self.frames_total += 1
            if x is None:
                self._break()
                continue
            self.frames_valid += 1
            self.frames.append(x)
            self.counter += 1
            if len(self.frames) == self.model.window and self.counter % self.model.stride == 0:
                out.append(self._predict(target))
        return out

    def _break(self) -> None:
        self.frames.clear()
        self.counter = 0

    def _predict(self, t: float) -> dict:
        xy, prob = self.model.predict(np.stack(self.frames)[None])
        xy, prob = xy[0], prob[0]
        self.predictions += 1
        gap = self.model.manifest.get("display", {}).get("reset_gap_sec", 0.6)
        if self.last_pred_t is not None and t - self.last_pred_t > gap:
            self.smoothed = None
        dt = 0.0 if self.last_pred_t is None else t - self.last_pred_t
        self.last_pred_t = t
        if self.smoothed is None or self.tau <= 0:
            self.smoothed = xy.copy()
        else:
            self.smoothed = self.smoothed + (1.0 - math.exp(-dt / self.tau)) * (xy - self.smoothed)
        return self.shaper.frame(t, xy, self.smoothed, prob)


def load(directory: Path, manifest: dict, verify: bool = True) -> UpperRuntime:
    scaler = np.load(check_file(directory, manifest["scaler"], verify))
    links, points, classes = manifest["links"], manifest["points"], manifest["classes"]
    nets = []
    for w in manifest["weights"]:
        net = UpperNet(len(links), len(classes), len(points))
        net.load_state_dict(torch.load(check_file(directory, w, verify), map_location="cpu", weights_only=True))
        net.eval()
        nets.append(net)
    model = UpperModel(manifest, scaler["median"].astype(np.float32), scaler["iqr"].astype(np.float32), nets)
    if model.median.shape != (model.n_features,) or model.iqr.shape != (model.n_features,):
        raise PoseModelError("scaler shape mismatch")
    return UpperRuntime(model)
