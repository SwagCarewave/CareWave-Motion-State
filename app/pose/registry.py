from __future__ import annotations

import hashlib
import json
from pathlib import Path


class PoseModelError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_file(directory: Path, spec: dict, verify: bool) -> Path:
    path = Path(directory) / spec["file"]
    if not path.is_file():
        raise PoseModelError(f"file not found: {path.name}")
    if verify and sha256(path) != spec["sha256"]:
        raise PoseModelError(f"hash mismatch: {path.name}")
    return path


def read_manifest(directory: Path) -> dict:
    path = Path(directory) / "manifest.json"
    if not path.is_file():
        raise PoseModelError(f"manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_runtime(directory: Path, verify: bool = True):
    from app.pose import tcn, upper

    manifest = read_manifest(directory)
    kinds = {"upper3_gru": upper.load, "csipose_tcn": tcn.load}
    kind = manifest.get("type")
    if kind not in kinds:
        raise PoseModelError(f"unknown model type: {kind}")
    return kinds[kind](Path(directory), manifest, verify)


def describe(manifest: dict) -> dict:
    from app.pose.output import ACTION_KO, DIRECTION_KO, POSTURE_KO

    return {
        "name": manifest["name"],
        "type": manifest.get("type"),
        "title": manifest.get("title", manifest["name"]),
        "points": manifest["points"],
        "skeleton": manifest.get("skeleton", []),
        "classes": manifest["classes"],
        "image_size": manifest.get("image_size"),
        "labels": {"action": {c: ACTION_KO.get(c, c) for c in manifest["classes"]}, "posture": POSTURE_KO,
                   "direction": DIRECTION_KO},
    }


def list_models(root: Path) -> list[dict]:
    out = []
    for path in sorted(Path(root).glob("*/manifest.json")):
        m = json.loads(path.read_text(encoding="utf-8"))
        out.append({"name": m["name"], "title": m.get("title", m["name"]), "type": m.get("type"),
                    "points": len(m.get("points", [])), "classes": m.get("classes", [])})
    return out
