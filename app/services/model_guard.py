from __future__ import annotations

import hashlib
import pickle
from dataclasses import dataclass
from pathlib import Path


class ModelIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelInfo:
    path: Path
    sha256: str
    model_type: str
    feature_count: int
    threshold: float
    smooth: int
    lookahead_sec: float

    def to_dict(self) -> dict:
        return {
            "name": self.path.stem,
            "sha256": self.sha256,
            "model_type": self.model_type,
            "feature_count": self.feature_count,
            "threshold": self.threshold,
            "smooth": self.smooth,
            "lookahead_sec": self.lookahead_sec,
        }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_model(path: Path, expected_sha256: str) -> ModelInfo:
    path = Path(path)
    if not path.is_file():
        raise ModelIntegrityError(f"model file not found: {path}")
    digest = file_sha256(path)
    if digest != expected_sha256:
        raise ModelIntegrityError(f"model hash mismatch: expected {expected_sha256}, got {digest}")
    bundle = pickle.loads(path.read_bytes())
    model = bundle["model"]
    final = model.steps[-1][1] if hasattr(model, "steps") else model
    return ModelInfo(
        path=path,
        sha256=digest,
        model_type=type(final).__name__,
        feature_count=len(bundle["columns"]),
        threshold=float(bundle["threshold"]),
        smooth=int(bundle["smooth"]),
        lookahead_sec=float(bundle["lookahead_sec"]),
    )
