from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path

import numpy as np
import pytest

from app.pose.features import FeatureBuilder
from app.pose.output import OutputShaper, _Hold
from app.pose.registry import PoseModelError, load_runtime
from app.pose.upper import UpperRuntime
from app.pose.packets import MAGIC, N_BINS, PacketError, parse_payload
from app.services.pose_live import PoseService
from tests.helpers import FakeClock

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models" / "pose"
ACTIVE = "carewave_v3_upper_20261102"
TCN = "csipose_tcn_v3_posture"


def record(peer: int, pairs: np.ndarray) -> bytes:
    rec = bytearray(352)
    rec[24] = peer
    struct.pack_into("<H", rec, 38, 256)
    rec[96:352] = pairs.astype(np.int8).tobytes()
    return bytes(rec)


def payload(owner: int, peers: list[int], seed: int = 0, kind: int = 1) -> bytes:
    rng = np.random.default_rng(seed)
    body = b"".join(record(p, rng.integers(-40, 40, size=(N_BINS, 2))) for p in peers)
    return MAGIC + bytes([kind, owner, len(peers), 0, 0]) + body


@pytest.fixture(scope="module")
def runtime():
    return load_runtime(MODELS / ACTIVE)


@pytest.fixture(scope="module")
def model(runtime):
    return runtime.model


def test_parse_payload_reads_links_and_amplitude():
    rng = np.random.default_rng(3)
    pairs = rng.integers(-40, 40, size=(N_BINS, 2))
    data = MAGIC + bytes([1, 2, 1, 0, 0]) + record(5, pairs)
    kind, records = parse_payload(data)
    assert kind == 1 and len(records) == 1
    r = records[0]
    assert r.link == "n2_p5"
    expected = np.hypot(pairs[:, 0], pairs[:, 1]).astype(np.float32)
    expected[:2] = 0
    assert np.allclose(r.amplitude, expected)


def test_parse_payload_rejects_bad_packets():
    good = payload(1, [2, 3])
    with pytest.raises(PacketError):
        parse_payload(good[:-1])
    with pytest.raises(PacketError):
        parse_payload(payload(2, [2]))
    with pytest.raises(PacketError):
        parse_payload(MAGIC + bytes([9, 1, 0, 0, 0]))
    assert parse_payload(MAGIC + bytes([2, 1, 1, 0, 0]) + bytes(112)) == (2, [])


def test_feature_layout_and_validity():
    links = ["n1_p2", "n2_p1"]
    b = FeatureBuilder(links, max_age=0.15)
    a1, a2 = np.arange(N_BINS, dtype=np.float32), np.full(N_BINS, 3, np.float32)
    b.add(10.00, "n1_p2", a1)
    assert b.frame(10.05) == (None, False)
    b.add(10.02, "n2_p1", a2)
    x, ok = b.frame(10.10)
    assert ok and x is None
    b.add(10.15, "n1_p2", a1 + 1)
    b.add(10.15, "n2_p1", a2)
    x, ok = b.frame(10.20)
    assert ok and x.shape == (2 * 2 * N_BINS + 2,)
    rel = x[:2 * N_BINS].reshape(2, N_BINS)
    assert np.allclose(rel[0], a1 - np.median(a1))
    motion = x[2 * N_BINS:4 * N_BINS].reshape(2, N_BINS)
    assert np.allclose(motion, 0)
    assert np.allclose(x[-2:], [np.median(a1 + 1), 3])
    assert b.frame(10.40) == (None, False)


def test_same_timestamp_records_use_median():
    b = FeatureBuilder(["n1_p2"], max_age=0.15)
    for v in (1.0, 5.0, 3.0):
        b.add(1.0, "n1_p2", np.full(N_BINS, v, np.float32))
    assert np.allclose(b.history["n1_p2"].at(1.0)[1], 3.0)


def test_hold_needs_stable_candidate():
    h = _Hold()
    assert h.update("upright", 0.0, 0.4) is None
    assert h.update("upright", 0.2, 0.4) is None
    assert h.update("upright", 0.4, 0.4) == "upright"
    assert h.update("lying", 0.6, 0.4) is None
    assert h.update(None, 0.8, 0.4) is None


def feed(engine, model, start: float, seconds: float, hz: float = 50.0, seed: int = 0, links=None) -> list[dict]:
    rng = np.random.default_rng(seed)
    frames = []
    for k in range(int(seconds * hz)):
        ts = start + k / hz
        frames.extend(engine.poll(ts - 0.1))
        for link in links or model.links:
            engine.add_amplitude(ts, link, rng.uniform(0, 40, N_BINS).astype(np.float32))
    return frames


def test_direction_follows_activity_thresholds(model):
    m = model.manifest
    shaper = OutputShaper(m["name"], m["points"], m["classes"], m["display"], m["game"], m["fall"])
    xy = np.zeros((3, 2), np.float32)
    for action, expected in {"stand": "up", "two_steps": "left", "fall": "right"}.items():
        prob = np.array([1.0 if c == action else 0.0 for c in m["classes"]])
        frame = shaper.frame(1.0, xy, xy, prob)
        assert frame["direction"] == expected and frame["activity_score"] == m["game"]["intensity"][action]


def test_engine_predicts_every_stride_after_window(model):
    engine = UpperRuntime(model)
    frames = feed(engine, model, 1000.0, 4.0)
    assert len(frames) >= 8
    gaps = np.round(np.diff([f["ts"] for f in frames]), 3)
    assert set(gaps.tolist()) == {0.2}
    f = frames[-1]
    assert [p["name"] for p in f["points"]] == model.points
    assert abs(sum(f["probabilities"].values()) - 1) < 1e-3
    assert f["posture"]["code"] in {"upright", "low", "lying", "uncertain"}
    assert f["direction"] in {"up", "left", "right"} and 0 <= f["activity_score"] <= 1


def test_engine_stops_when_a_link_goes_quiet(model):
    engine = UpperRuntime(model)
    assert feed(engine, model, 1000.0, 3.0)
    later = feed(engine, model, 1003.0, 1.0, links=model.links[1:])
    assert all(f["ts"] <= 1003.0 + model.max_age for f in later)
    assert engine.poll(1003.95) == []


def test_model_hash_mismatch_is_rejected(tmp_path):
    target = tmp_path / ACTIVE
    shutil.copytree(MODELS / ACTIVE, target)
    m = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    m["weights"][0]["sha256"] = "0" * 64
    (target / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(PoseModelError):
        load_runtime(target)


def test_service_ticks_into_subscribers_and_links(model):
    clock = FakeClock(2000.0)
    service = PoseService(MODELS, ACTIVE, clock=clock)
    rng = np.random.default_rng(0)
    from app.pose.packets import LinkRecord
    for k in range(200):
        ts = 2000.0 + k / 50
        recs = [LinkRecord(int(l[1]), int(l[4]), record(int(l[4]), rng.integers(-40, 40, size=(N_BINS, 2))))
                for l in model.links]
        service.ingest(ts, recs)
    clock.now = 2004.1
    service.tick()
    status = service.status()
    assert status["latest"] is not None
    assert status["links_total"] == 30
    assert status["counters"]["records"] == 200 * 30


def test_udp_routes_mesh_and_text(settings):
    from app.services.udp_ingest import UdpCollector

    class Sink:
        def __init__(self):
            self.got = []

        def ingest(self, ts, records):
            self.got.append((ts, records))

    sink = Sink()
    collector = UdpCollector(settings, sessions=None, clock=lambda: 5.0, pose=sink)
    collector.datagram_received(payload(1, [2, 3, 4]), ("127.0.0.1", 1))
    collector.datagram_received(b"not csi\n", ("127.0.0.1", 1))
    assert len(sink.got) == 1 and [r.link for r in sink.got[0][1]] == ["n1_p2", "n1_p3", "n1_p4"]
    st = collector.status()
    assert st["mesh_packets"] == 1 and st["mesh_records"] == 3 and st["rejected"] == 1


def test_pose_api(client):
    r = client.get("/api/pose/status")
    assert r.status_code == 200
    body = r.json()
    assert body["model"]["name"] == TCN and len(body["model"]["points"]) == 13 and body["model"]["skeleton"]
    assert body["links_total"] == 30 and body["ready"] is False and body["calibration"]["required"] is True
    assert body["links"][0]["link"] == "n1_p2" and body["links"][0]["from"] == 2 and body["links"][0]["to"] == 1
    assert client.get("/api/pose/latest").json() == {"latest": None}
    models = {m["name"]: m for m in client.get("/api/pose/models").json()}
    assert models[TCN]["active"] and models[TCN]["points"] == 13 and models[TCN]["type"] == "csipose_tcn"
    assert models[ACTIVE]["points"] == 3 and not models[ACTIVE]["active"]
    r = client.put("/api/pose/models/active", json={"name": "missing"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "pose_model_not_found"
    with client.websocket_connect("/ws/pose") as ws:
        snap = ws.receive_json()
    assert snap["type"] == "snapshot" and snap["model"]["name"] == TCN and snap["calibration"]["active"]


def test_pose_disabled(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    s = Settings(storage_backend="memory", local_storage_dir=tmp_path / "s", pose_enabled=False)
    with TestClient(create_app(s)) as c:
        r = c.get("/api/pose/status")
    assert r.status_code == 503 and r.json()["error"]["code"] == "pose_unavailable"


def tcn_entry(tx: int, rng, kind: int = 6, rssi: int = -40) -> bytes:
    rec = bytearray(352)
    rec[24], rec[25], rec[26] = tx, kind, rssi & 0xFF
    struct.pack_into("<H", rec, 38, 256)
    rec[96:352] = rng.integers(-40, 40, size=(128, 2)).astype(np.int8).tobytes()
    return bytes(rec)


def feed_tcn(rt, start: float, seconds: float, hz: float = 50.0, seed: int = 0, skip=()) -> list[dict]:
    from app.pose.packets import LinkRecord

    rng = np.random.default_rng(seed)
    out = []
    for k in range(int(seconds * hz)):
        ts = start + k / hz
        out.extend(rt.poll(ts))
        for rx in range(1, 7):
            for tx in range(1, 7):
                if tx != rx and (tx, rx) not in skip:
                    rt.add(ts, LinkRecord(rx, tx, tcn_entry(tx, rng)))
    return out


def test_tcn_runtime_calibrates_then_streams_13_joints():
    rt = load_runtime(MODELS / TCN)
    assert rt.calibration()["active"]
    early = feed_tcn(rt, 100.0, 9.5)
    assert early == [] and 0.9 < rt.calibration()["progress"] < 1.0
    later = feed_tcn(rt, 109.5, 2.0, seed=1)
    assert not rt.calibration()["active"] and len(later) >= 25
    f = later[-1]
    assert len(f["points"]) == 13 and f["points"][0]["name"] == "nose"
    assert f["pixels"] == {"width": 1920, "height": 1080} and f["links_seen"] == 30
    assert abs(sum(f["probabilities"].values()) - 1) < 1e-3
    assert f["direction"] in {"up", "left", "right"} and isinstance(f["fall_alarm"], bool)
    rt.reset()
    assert rt.calibration()["active"] and rt.predictions == 0


def test_tcn_entry_feature_matches_reference_layout():
    from app.pose.tcn import HT_INDICES, entry_feature

    rng = np.random.default_rng(5)
    entry = tcn_entry(3, rng, rssi=-55)
    iq = np.frombuffer(entry[96:352], dtype=np.int8).astype(np.float32).reshape(128, 2)
    amp = np.abs(iq[HT_INDICES, 1] + 1j * iq[HT_INDICES, 0])
    amp = amp / amp.mean()
    feat = entry_feature(entry, 4)
    assert feat.shape == (15,)
    assert np.allclose(feat[:14], amp.reshape(14, 4).mean(1), atol=1e-5)
    assert np.isclose(feat[14], -55 / 50 + 1)


def test_switch_model_and_recalibrate_api(client):
    r = client.post("/api/pose/calibration")
    assert r.status_code == 200 and r.json()["active"] is True and r.json()["seconds"] == 10.0
    r = client.put("/api/pose/models/active", json={"name": ACTIVE})
    assert r.status_code == 200 and len(r.json()["points"]) == 3
    status = client.get("/api/pose/status").json()
    assert status["model"]["name"] == ACTIVE and status["calibration"]["required"] is False
    assert client.post("/api/pose/calibration").json()["required"] is False
    assert client.put("/api/pose/models/active", json={"name": TCN}).status_code == 200
    assert client.get("/api/pose/status").json()["model"]["name"] == TCN
