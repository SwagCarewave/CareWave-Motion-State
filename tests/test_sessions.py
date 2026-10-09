from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from helpers import FakeClock, csv_packets, send_packets, start_session

from app.config import Settings, load_settings
from app.repositories import memory_repositories, supabase_repositories
from app.services.monitoring import SessionManager
from motion_state.monitor_v2 import NightMonitorV2


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
    packets = csv_packets(sample_csv)
    sid = start_session(app_client)
    totals = send_packets(app_client, sid, packets)
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
    packets = csv_packets(sample_csv, 200)
    sid = start_session(app_client)
    first = send_packets(app_client, sid, packets)
    assert first["accepted"] == 200
    again = send_packets(app_client, sid, packets)
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
    sid = start_session(app_client)
    send_packets(app_client, sid, csv_packets(sample_csv))
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
    sid = start_session(app_client)
    packets = csv_packets(sample_csv, 1500)
    send_packets(app_client, sid, packets)
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
    sid = start_session(app_client)
    send_packets(app_client, sid, csv_packets(sample_csv))
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
    packets = csv_packets(sample_csv, 1500)
    first.ingest(row["id"], packets)
    clock.now = packets[-1]["ts"]
    first.tick()
    saved = len(first.live[row["id"]].frames)

    second = SessionManager(live_settings, repos, "sha", clock)
    assert second.status(row["id"])["status"] == "running"
    assert second.signals(row["id"], window_sec=3600)["count"] == saved
    more = [{**p, "ts": p["ts"] + 200} for p in csv_packets(sample_csv, 400)]
    clock.now = more[-1]["ts"]
    assert second.ingest(row["id"], more).accepted == 400
    assert second.start()[0]["id"] == row["id"]


def test_websocket_streams_frames(app_client, sample_csv):
    sid = start_session(app_client)
    packets = csv_packets(sample_csv, 600)
    with app_client.websocket_connect(f"/ws/sessions/{sid}") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        assert snap["session"]["id"] == sid
        send_packets(app_client, sid, packets)
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
        packets = csv_packets(sample_csv, 900)
        clock.now = packets[-1]["ts"]
        assert manager.ingest(row["id"], packets).accepted == 900
        manager.tick()
        assert len(repos.device_status.list({"session_id": row["id"]})) == 3
        manager.stop(row["id"])
        stored = manager.signals(row["id"], window_sec=3600)
        assert stored["count"] == len(repos.signal_frames.list({"session_id": row["id"]}))
        assert stored["count"] > 0
        assert repos.sessions.get(row["id"])["status"] == "stopped"
        events = repos.activity_events.list({"session_id": row["id"]})
        assert [e["event_no"] for e in events] == [1]
        again = {k: events[0][k] for k in ("session_id", "analysis_id", "event_no", "started_at", "alerted_at",
                                           "ended_at", "duration_sec", "alert_message")}
        repos.activity_events.upsert(again, ("session_id", "analysis_id", "event_no"))
        assert len(repos.activity_events.list({"session_id": row["id"]})) == 1
        assert manager.status(row["id"])["event_count"] == 1
    finally:
        repos.sessions.delete(row["id"])
    assert repos.signal_frames.list({"session_id": row["id"]}) == []


class FlakyTable:
    def __init__(self, inner, fail_update: int = 0, fail_insert: int = 0):
        self.inner = inner
        self.fail_update = fail_update
        self.fail_insert = fail_insert

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def update(self, *args, **kwargs):
        if self.fail_update:
            self.fail_update -= 1
            raise RuntimeError("update failed")
        return self.inner.update(*args, **kwargs)

    def insert_many(self, rows):
        result = self.inner.insert_many(rows)
        if self.fail_insert:
            self.fail_insert -= 1
            raise RuntimeError("response lost after insert")
        return result


def _manager(live_settings, repos=None):
    repos = repos or memory_repositories(live_settings.local_storage_dir)
    clock = FakeClock()
    return SessionManager(live_settings, repos, "sha", clock), repos, clock


def test_restart_rejects_resent_packets_and_continues_event_numbers(live_settings, sample_csv):
    first, repos, clock = _manager(live_settings)
    row, _ = first.start()
    packets = csv_packets(sample_csv)
    clock.now = packets[2999]["ts"]
    first.ingest(row["id"], packets[:3000])
    first.tick()
    stored_frames = len(repos.signal_frames.list({"session_id": row["id"]}))
    stored_events = sorted(e["event_no"] for e in repos.activity_events.list({"session_id": row["id"]}))
    assert stored_events == [e["id"] for e in first.live[row["id"]].engine.events()]
    assert stored_events

    second = SessionManager(live_settings, repos, "sha", clock)
    resent = second.ingest(row["id"], packets[2000:3000])
    assert resent.accepted == 0
    assert resent.rejected_stale == 1000
    second.tick()
    assert len(repos.signal_frames.list({"session_id": row["id"]})) == stored_frames

    clock.now = packets[-1]["ts"]
    second.ingest(row["id"], packets[3000:])
    second.stop(row["id"])
    numbers = sorted(e["event_no"] for e in repos.activity_events.list({"session_id": row["id"]}))
    assert numbers == sorted(set(numbers))
    assert numbers[0] == 1


def test_restart_shows_stored_receiver_state(live_settings, sample_csv):
    first, repos, clock = _manager(live_settings)
    row, _ = first.start()
    packets = csv_packets(sample_csv, 900)
    clock.now = packets[-1]["ts"]
    first.ingest(row["id"], packets)
    first.tick()

    clock.now = packets[-1]["ts"] + 60
    second = SessionManager(live_settings, repos, "sha", clock)
    rx = second.status(row["id"])["rx"]
    assert {r["status"] for r in rx} == {"lost"}
    assert all(r["last_packet_ts"] is not None for r in rx)


def test_stop_keeps_events_and_final_state(live_settings, sample_csv):
    manager, repos, clock = _manager(live_settings)
    row, _ = manager.start()
    packets = csv_packets(sample_csv)
    clock.now = packets[-1]["ts"]
    manager.ingest(row["id"], packets)
    live_events = manager.live[row["id"]].engine.events()
    manager.stop(row["id"])

    stored = repos.activity_events.list({"session_id": row["id"]}, order_by="event_no")
    assert [r["event_no"] for r in stored] == [e["id"] for e in live_events] == [1, 2]
    assert stored[0]["alert_message"].startswith("취침 모드 중 움직임이")

    status = manager.status(row["id"])
    assert status["status"] == "stopped"
    assert status["event_count"] == 2
    assert status["unconfirmed_count"] == 2
    assert status["state"] is not None
    assert {r["status"] for r in status["rx"]} == {"ok"}
    signals = manager.signals(row["id"], window_sec=3600)
    assert [e["id"] for e in signals["events"]] == [1, 2]


def test_flush_does_not_duplicate_frames_on_partial_failure(live_settings, sample_csv):
    base = memory_repositories(live_settings.local_storage_dir)
    flaky_sessions = FlakyTable(base.sessions, fail_update=2)
    flaky_frames = FlakyTable(base.signal_frames, fail_insert=1)
    from dataclasses import replace
    repos = replace(base, sessions=flaky_sessions, signal_frames=flaky_frames)
    manager, _, clock = _manager(live_settings, repos)
    row, _ = manager.start()
    packets = csv_packets(sample_csv, 1500)
    clock.now = packets[-1]["ts"]
    manager.ingest(row["id"], packets)

    manager.tick()
    clock.now += 5
    manager.tick()
    clock.now += 5
    manager.tick()
    expected = len(manager.live[row["id"]].frames)
    assert not manager.live[row["id"]].pending
    rows = base.signal_frames.list({"session_id": row["id"]})
    assert len(rows) == expected
    assert len({r["ts"] for r in rows}) == expected
    assert base.sessions.get(row["id"])["last_packet_at"] is not None


def test_future_and_huge_gap_timestamps_are_safe(app_client, sample_csv):
    import time

    sid = start_session(app_client)
    packets = csv_packets(sample_csv, 900)
    send_packets(app_client, sid, packets)
    last = packets[-1]["ts"]

    future = app_client.post(f"/api/sessions/{sid}/packets", json={"packets": [
        {"ts": app_client.clock.now + 7200, "rx": "RX1", "amplitude": [5.0] * 52}]}).json()
    assert future["rejected_invalid"] == 1

    jump = [{**p, "ts": p["ts"] + 86400} for p in packets[:300]]
    app_client.clock.now = jump[-1]["ts"]
    started = time.monotonic()
    res = app_client.post(f"/api/sessions/{sid}/packets", json={"packets": jump}).json()
    assert time.monotonic() - started < 5
    assert res["accepted"] == 300
    status = app_client.get(f"/api/sessions/{sid}").json()
    assert status["last_packet_at"] > last + 86000


def test_iso_timestamps_keep_sub_millisecond_order(app_client, sample_csv):
    from app.timeutil import parse_ts, to_iso

    packets = [{**p, "ts": to_iso(p["ts"])} for p in csv_packets(sample_csv)]
    last: dict[str, float] = {}
    collide = 0
    for p in packets:
        rounded = round(parse_ts(p["ts"]), 3)
        collide += p["rx"] in last and rounded <= last[p["rx"]]
        last[p["rx"]] = rounded
    assert collide > 0

    sid = start_session(app_client)
    accepted = stale = 0
    for i in range(0, len(packets), 300):
        batch = packets[i:i + 300]
        app_client.clock.now = parse_ts(batch[-1]["ts"])
        body = app_client.post(f"/api/sessions/{sid}/packets", json={"packets": batch}).json()
        accepted += body["accepted"]
        stale += body["rejected_stale"]
    assert stale == 0
    assert accepted == len(packets)

    mon = NightMonitorV2(Settings().model_path)
    ref = []
    for p in packets:
        ts = parse_ts(p["ts"])
        mon.push_packet(ts, p["rx"], np.asarray(p["amplitude"]))
        ref.extend(mon.poll(ts))
    frames = list(app_client.app.state.sessions.live[sid].frames)
    assert [(f.state, f.activity_score) for f in frames] == [(o.state, o.activity_score) for o in ref]
    assert app_client.get(f"/api/sessions/{sid}").json()["event_count"] == len(mon.events) == 2


def test_same_timestamp_different_packets_are_kept(app_client):
    sid = start_session(app_client)
    t = 1782371762.5
    first = {"ts": t, "rx": "RX1", "amplitude": [1.0] * 52}
    second = {"ts": t, "rx": "RX1", "amplitude": [2.0] * 52}
    app_client.clock.now = t
    body = app_client.post(f"/api/sessions/{sid}/packets", json={"packets": [first, second]}).json()
    assert body["accepted"] == 2 and body["rejected_stale"] == 0
    again = app_client.post(f"/api/sessions/{sid}/packets", json={"packets": [first, second]}).json()
    assert again["accepted"] == 0 and again["rejected_stale"] == 2
    later = app_client.post(f"/api/sessions/{sid}/packets",
                            json={"packets": [{**first, "ts": t + 0.03}, first]}).json()
    assert later["accepted"] == 1 and later["rejected_stale"] == 1


def test_latest_recording_with_shared_timestamps_matches_engine(app_client):
    from app.timeutil import parse_ts, to_iso

    path = Path(__file__).resolve().parent.parent / "data" / "raw_csi" / "test" / "sujin_walk_stop_fall_01_csi_raw.csv"
    packets = [{**p, "ts": to_iso(p["ts"])} for p in csv_packets(path)]
    sid = start_session(app_client)
    stale = accepted = 0
    for i in range(0, len(packets), 300):
        batch = packets[i:i + 300]
        app_client.clock.now = parse_ts(batch[-1]["ts"])
        body = app_client.post(f"/api/sessions/{sid}/packets", json={"packets": batch}).json()
        stale += body["rejected_stale"]
        accepted += body["accepted"]
    assert stale == 0 and accepted == len(packets)

    mon = NightMonitorV2(Settings().model_path)
    ref = []
    for p in packets:
        ts = parse_ts(p["ts"])
        mon.push_packet(ts, p["rx"], np.asarray(p["amplitude"]))
        ref.extend(mon.poll(ts))
    frames = list(app_client.app.state.sessions.live[sid].frames)
    assert [(f.state, f.activity_score) for f in frames] == [(o.state, o.activity_score) for o in ref]
