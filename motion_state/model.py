"""Static/dynamic classifier on window features.

Adds causal per-recording relative features: each motion feature minus its 10th percentile
over the past 60 s of the same stream. This removes the room/day noise floor. Until 10 s of
history exist, the floor is blended toward the training static median.
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .features import HOP_SEC

META = ["sample_id", "date", "person", "t_end", "t_center", "label", "cls", "dchg", "boundary"]
FLOOR_SEC = 60.0
FLOOR_QUANTILE = 0.10
WARMUP_SEC = 10.0
STATIC_FA = 0.03


def base_feature_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in META and not c.startswith("rel_")]


def add_relative_features(df: pd.DataFrame, prior: dict[str, float]) -> pd.DataFrame:
    """Rows must be in time order within each sample_id."""
    out = df.copy()
    n_floor = int(FLOOR_SEC / HOP_SEC)
    n = out.groupby("sample_id").cumcount().to_numpy() + 1
    w = np.minimum(n / (WARMUP_SEC / HOP_SEC), 1.0)
    for c, p in prior.items():
        floor = out.groupby("sample_id")[c].transform(
            lambda x: x.rolling(n_floor, min_periods=1).quantile(FLOOR_QUANTILE)).to_numpy()
        out[f"rel_{c}"] = out[c].to_numpy() - (w * floor + (1 - w) * p)
    return out


class MotionStateModel:
    def __init__(self, static_fa: float = STATIC_FA, C: float = 0.3):
        self.static_fa = static_fa
        self.C = C

    def fit(self, train: pd.DataFrame) -> "MotionStateModel":
        """train: all windows of the training recordings (used whole so the floor is causal)."""
        base = base_feature_cols(train)
        static = train.cls == "static"
        self.prior = {c: float(train.loc[static, c].median()) for c in base if c.endswith("_med")}
        d = add_relative_features(train, self.prior)
        self.cols = base + [f"rel_{c}" for c in self.prior]
        m = (d.cls.isin(["static", "dynamic"]) & ~d.boundary).to_numpy()
        y = (d.cls[m] == "dynamic").to_numpy()
        w = np.where(y, 0.5 / y.mean(), 0.5 / (1 - y.mean()))
        self.clf = make_pipeline(StandardScaler(), LogisticRegression(C=self.C, max_iter=3000))
        self.clf.fit(d.loc[m, self.cols], y, logisticregression__sample_weight=w)
        s_static = self.clf.predict_proba(d.loc[m & static.to_numpy(), self.cols])[:, 1]
        self.threshold = float(np.quantile(s_static, 1 - self.static_fa))
        return self

    def score(self, df: pd.DataFrame) -> np.ndarray:
        d = add_relative_features(df, self.prior)
        return self.clf.predict_proba(d[self.cols])[:, 1]

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        return self.score(df) >= self.threshold

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pickle.dumps(self))

    @staticmethod
    def load(path: Path) -> "MotionStateModel":
        return pickle.loads(path.read_bytes())
