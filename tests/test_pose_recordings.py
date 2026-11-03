from __future__ import annotations

import io
import struct
import time

import numpy as np
import pytest

from app.pose.packets import FILE_HEADER, MAGIC


def entry(tx: int, rng) -> bytes:
    rec = bytearray(352)
    rec[24], rec[25], rec[26] = tx, 6, (-40) & 0xFF
    struct.pack_into("<H", rec, 38, 256)
    rec[96:352] = rng.integers(-40, 40, size=(128, 2)).astype(np.int8).tobytes()
    return bytes(rec)


def recording(seconds: float, start: float = 1_793_000_000.0, hz: float = 25.0, seed: int = 0) -> bytes:
    rng = np.random.default_rng(seed)
    out = bytearray()
    for k in range(int(seconds * hz)):
        ts = start + k / hz
        for rx in range(1, 7):
            peers = [tx for tx in range(1, 7) if tx != rx]
            payload = MAGIC + bytes([1, rx, len(peers), 0, 0]) + b"".join(entry(tx, rng) for tx in peers)
            out += FILE_HEADER.pack(ts + rx * 0.001, len(payload)) + payload
    return bytes(out)


def wait_analysis(client, analysis_id: str, timeout: float = 60.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        body = client.get(f"/api/analyses/{analysis_id}").json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.2)
    raise AssertionError("analysis did not finish")


@pytest.fixture
def uploaded(client):
    data = recording(14.0)
    r = client.post("/api/files", files={"file": ("test_6node.bin", io.BytesIO(data), "application/octet-stream")})
    assert r.status_code == 201, r.text
    return r.json()


def test_recording_upload_is_analyzed_into_skeleton_frames(client, settings, uploaded):
    f = uploaded
    assert f["kind"] == "mesh6" and f["receivers"] == ["N1", "N2", "N3", "N4", "N5", "N6"]
    assert f["rows"] == int(14.0 * 25) * 30 and 13.9 < f["duration_sec"] < 14.0
    assert f["latest_analysis"] is not None
    a = wait_analysis(client, f["latest_analysis"]["id"])
    assert a["status"] == "succeeded", a
    assert a["kind"] == "pose" and a["pose_model"]["name"] == "csipose_tcn_v3_posture"
    assert len(a["pose_model"]["points"]) == 13 and a["pose_model"]["skeleton"]
    assert 60 <= a["frame_count"] <= 85
    assert not settings.recording_uploads_path.joinpath(f"{f['id']}.bin").exists()
    listed = {x["id"]: x for x in client.get("/api/files").json()}
    assert listed[f["id"]]["kind"] == "mesh6"
    r = client.get(f"/api/analyses/{a['id']}/signals")
    assert r.status_code == 409 and r.json()["error"]["code"] == "pose_analysis"


def test_recording_replay_follows_slider(client, uploaded):
    a = wait_analysis(client, uploaded["latest_analysis"]["id"])
    replay = client.post("/api/replays", json={"analysis_id": a["id"]}).json()
    start, end = replay["record_start"], replay["record_end"]
    with client.websocket_connect(f"/ws/replays/{replay['id']}") as ws:
        snap = ws.receive_json()
        assert snap["model"]["name"] == "csipose_tcn_v3_posture"
        target = start + 12.0
        client.patch(f"/api/replays/{replay['id']}", json={"position_ts": target})
        tick = None
        while tick is None:
            m = ws.receive_json()
            if m["type"] == "tick":
                tick = m
    current = tick["current"]
    assert current is not None and current["ts"] <= target and target - current["ts"] < 0.1
    assert len(current["smoothed"]) == 13 and current["direction"] in {"up", "left", "right"}
    assert start <= end


def test_recording_reanalysis_needs_reupload_and_delete(client, uploaded):
    a = wait_analysis(client, uploaded["latest_analysis"]["id"])
    r = client.post(f"/api/files/{uploaded['id']}/analyses")
    again = wait_analysis(client, r.json()["id"])
    assert again["status"] == "failed" and again["error_code"] == "recording_missing"
    assert client.get(f"/api/analyses/{a['id']}").status_code in (200, 404)
    assert client.delete(f"/api/files/{uploaded['id']}").status_code == 204
    assert client.get(f"/api/files/{uploaded['id']}").status_code == 404


def test_recording_rejects_wrong_files(client):
    r = client.post("/api/files", files={"file": ("bad.bin", io.BytesIO(b"not a recording at all" * 10), "application/octet-stream")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_format"
    r = client.post("/api/files", files={"file": ("bad.txt", io.BytesIO(b"x"), "text/plain")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_file"
