from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.main import create_app
from app.repositories import memory_repositories, supabase_repositories
from app.services.csv_reader import load_csi_csv
from app.services.monitoring import SessionManager
from motion_state.monitor_v2 import NightMonitorV2


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def live_settings(tmp_path):
    return Settings(storage_backend="memory", local_storage_dir=tmp_path / "storage", tick_sec=3600)


@pytest.fixture
def app_client(live_settings):
    with TestClient(create_app(live_settings)) as c:
        c.clock = FakeClock()
        c.app.state.sessions.clock = c.clock
        yield c


def _packets(sample_csv, limit: int | None = None):
    table = load_csi_csv(sample_csv)
    out = []
    for t, rx, amp in table.packets():
        out.append({"ts": table.origin_ts + t, "rx": rx, "amplitude": amp.tolist()})
    return out[:limit] if limit else out


def _send(client, session_id, packets, size=300):
    totals = {"accepted": 0, "rejected_invalid": 0, "rejected_incomplete": 0, "rejected_stale": 0, "frames": 0}
    for i in range(0, len(packets), size):
        batch = packets[i:i + size]
        client.clock.now = batch[-1]["ts"]
        res = client.post(f"/api/sessions/{session_id}/packets", json={"packets": batch})
        assert res.status_code == 200, res.text
        for k, v in res.json().items():
            totals[k] += v
    return totals


def _start(client) -> str:
    res = client.post("/api/sessions")
    assert res.status_code in (200, 201)
    return res.json()["id"]


def test_start_is_idempotent(app_client):
    first = app_client.post("/api/sessions")
    assert first.status_code == 201
    assert first.json()["created"] is True
    again = app_client.post("/api/sessions")
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["id"] == first.json()["id"]
    running = app_client.get("/api/sessions", params={"status": "running"}).json()
    assert [s["id"] for s in running] == [first.json()["id"]]
    assert {r["status"] for r in first.json()["rx"]} == {"none"}


def test_ingest_matches_original_monitor(app_client, sample_csv):
    packets = _packets(sample_csv)
    sid = _start(app_client)
    totals = _send(app_client, sid, packets)
    assert totals["accepted"] == len(packets)

    mon = NightMonitorV2(Settings().model_path)
    ref = []
    for p in packets:
        mon.push_packet(p["ts"], p["rx"], np.asarray(p["amplitude"]))
        ref.extend(mon.poll(p["ts"]))

    live = app_client.app.state.sessions.live[sid]
    frames = list(live.frames)
    assert len(frames) == len(ref) == totals["frames"]
    for f, o in zip(frames, ref):
        assert (f.state, f.activity_score, f.motion_index, f.alert) == (o.state, o.activity_score, o.motion_index,
                                                                        o.alert)
    status = app_client.get(f"/api/sessions/{sid}").json()
    assert status["event_count"] == len(mon.events)
    assert {r["status"] for r in status["rx"]} == {"ok"}
    assert status["seconds_since_last_packet"] == 0.0


def test_rejects_bad_duplicate_and_old_packets(app_client, sample_csv):
    packets = _packets(sample_csv, 200)
    sid = _start(app_client)
    first = _send(app_client, sid, packets)
    assert first["accepted"] == 200
    again = _send(app_client, sid, packets)
    assert again["accepted"] == 0
    assert again["rejected_stale"] == 200

    t = packets[-1]["ts"] + 1
    res = app_client.post(f"/api/sessions/{sid}/packets", json={"packets": [
        {"ts": t, "rx": "RX9", "amplitude": [1.0] * 52},
        {"ts": "not-a-time", "rx": "RX1", "amplitude": [1.0] * 52},
        {"ts": t, "rx": "RX1", "amplitude": [1.0] * 51},
        {"ts": t, "rx": "RX1", "amplitude": [1.0] * 51 + [None]},
        {"ts": "2026-06-25T07:10:00+00:00", "rx": "RX2", "amplitude": [1.0] * 52},
        {"rx": "RX3", "amplitude": [1.0] * 52},
    ]})
    body = res.json()
    assert body == {"accepted": 1, "rejected_invalid": 2, "rejected_incomplete": 2, "rejected_stale": 1,
                    "frames": body["frames"]}


def test_signals_window_and_downsampling(app_client, sample_csv):
    sid = _start(app_client)
    _send(app_client, sid, _packets(sample_csv))
    full = app_client.get(f"/api/sessions/{sid}/signals", params={"window": 60}).json()
    assert 115 <= full["count"] <= 121
    assert full["step_sec"] == 0.5
    assert len(full["heatmap"][-1]) == 52
    assert full["activity_threshold"] == pytest.approx(0.5, abs=1e-3)
    assert [e["id"] for e in full["events"]] == [2]
    everything = app_client.get(f"/api/sessions/{sid}/signals", params={"window": 3600}).json()
    assert [e["id"] for e in everything["events"]] == [1, 2]

    small = app_client.get(f"/api/sessions/{sid}/signals",
                           params={"window": 600, "max_points": 20, "heatmap": False}).json()
    assert small["count"] <= 20
    assert small["step_sec"] > 0.5
    assert small["heatmap"] is None
    assert len(small["ts"]) == len(small["motion_index"]) == small["count"]


def test_rx_lost_and_signal_check_when_packets_stop(app_client, sample_csv):
    sid = _start(app_client)
    packets = _packets(sample_csv, 1500)
    _send(app_client, sid, packets)
    manager = app_client.app.state.sessions
    before = len(manager.live[sid].frames)

    app_client.clock.now = packets[-1]["ts"] + 12
    manager.tick()
    status = app_client.get(f"/api/sessions/{sid}").json()
    assert {r["status"] for r in status["rx"]} == {"lost"}
    assert status["seconds_since_last_packet"] == 12.0
    frames = list(manager.live[sid].frames)[before:]
    assert frames and frames[-1].state == "signal_check"

    stored = app_client.app.state.repos.device_status.list({"session_id": sid})
    assert {r["status"] for r in stored} == {"lost"}

    late = app_client.post(f"/api/sessions/{sid}/packets",
                           json={"packets": [{"ts": packets[-1]["ts"] + 1, "rx": "RX1", "amplitude": [5.0] * 52}]})
    assert late.json()["rejected_stale"] == 1


def test_stop_persists_and_reads_from_storage(app_client, sample_csv):
    sid = _start(app_client)
    _send(app_client, sid, _packets(sample_csv))
    live_count = app_client.get(f"/api/sessions/{sid}/signals", params={"window": 3600}).json()["count"]

    stopped = app_client.post(f"/api/sessions/{sid}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"
    assert app_client.post(f"/api/sessions/{sid}/stop").status_code == 200
    res = app_client.post(f"/api/sessions/{sid}/packets", json={"packets": []})
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "session_stopped"

    stored = app_client.get(f"/api/sessions/{sid}/signals", params={"window": 3600}).json()
    assert stored["count"] >= live_count
    assert all(len(h) == 52 for h in stored["heatmap"] if h is not None)
    assert sum(h is not None for h in stored["heatmap"]) >= live_count - 2
    assert app_client.post("/api/sessions").json()["id"] != sid


def test_restart_resumes_running_session(live_settings, sample_csv):
    repos = memory_repositories(live_settings.local_storage_dir)
    clock = FakeClock()
    first = SessionManager(live_settings, repos, "sha", clock)
    row, _ = first.start()
    packets = _packets(sample_csv, 1500)
    first.ingest(row["id"], packets)
    clock.now = packets[-1]["ts"]
    first.tick()
    saved = len(first.live[row["id"]].frames)

    second = SessionManager(live_settings, repos, "sha", clock)
    assert second.status(row["id"])["status"] == "running"
    assert second.signals(row["id"], window_sec=3600)["count"] == saved
    more = [{**p, "ts": p["ts"] + 200} for p in _packets(sample_csv, 400)]
    clock.now = more[-1]["ts"]
    assert second.ingest(row["id"], more).accepted == 400
    assert second.start()[0]["id"] == row["id"]


def test_websocket_streams_frames(app_client, sample_csv):
    sid = _start(app_client)
    packets = _packets(sample_csv, 600)
    with app_client.websocket_connect(f"/ws/sessions/{sid}") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        assert snap["session"]["id"] == sid
        _send(app_client, sid, packets)
        seen = []
        while sum(len(m.get("frames", [])) for m in seen) < 10:
            seen.append(ws.receive_json())
        frames = [f for m in seen if m["type"] == "frames" for f in m["frames"]]
        assert frames[0]["state"]
        app_client.post(f"/api/sessions/{sid}/stop")
        kinds = set()
        while "stopped" not in kinds:
            kinds.add(ws.receive_json()["type"])


def test_unknown_session(app_client):
    res = app_client.get("/api/sessions/00000000-0000-0000-0000-000000000000")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "session_not_found"
    with app_client.websocket_connect("/ws/sessions/nope") as ws:
        with pytest.raises(Exception):
            ws.receive_json()


@pytest.mark.supabase
def test_supabase_session_roundtrip(tmp_path, sample_csv):
    s = load_settings()
    if not (s.supabase_url and s.supabase_key):
        pytest.skip("Supabase 설정 없음")
    repos = supabase_repositories(s.supabase_url, s.supabase_key, s.csv_bucket, s.results_bucket)
    for old in repos.sessions.list({"status": "running", "model_sha256": "pytest"}):
        repos.sessions.delete(old["id"])
    settings = Settings(storage_backend="supabase", local_storage_dir=tmp_path, tick_sec=3600)
    clock = FakeClock()
    manager = SessionManager(settings, repos, "pytest", clock)
    if repos.sessions.list({"status": "running"}):
        pytest.skip("실제 실행 중인 세션이 있어 건너뜀")
    row, created = manager.start()
    try:
        assert created
        packets = _packets(sample_csv, 900)
        clock.now = packets[-1]["ts"]
        assert manager.ingest(row["id"], packets).accepted == 900
        manager.tick()
        assert len(repos.device_status.list({"session_id": row["id"]})) == 3
        manager.stop(row["id"])
        stored = manager.signals(row["id"], window_sec=3600)
        assert stored["count"] == len(repos.signal_frames.list({"session_id": row["id"]}))
        assert stored["count"] > 0
        assert repos.sessions.get(row["id"])["status"] == "stopped"
    finally:
        repos.sessions.delete(row["id"])
    assert repos.signal_frames.list({"session_id": row["id"]}) == []
