from __future__ import annotations

import io
import time
from dataclasses import replace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from helpers import FakeClock, csv_packets, send_packets, start_session

from app.main import create_app
from app.services.csv_reader import load_csi_csv


def _client(settings):
    client = TestClient(create_app(settings))
    client.__enter__()
    client.clock = FakeClock()
    client.app.state.sessions.clock = client.clock
    client.app.state.events.clock = client.clock
    return client


@pytest.fixture
def rec_client(live_settings):
    client = _client(live_settings)
    yield client
    client.__exit__(None, None, None)


def _record(client, packets):
    sid = start_session(client)
    send_packets(client, sid, packets)
    return sid


def _stop(client, sid):
    assert client.post(f"/api/sessions/{sid}/stop").status_code == 200


def _wait(client, analysis_id, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        body = client.get(f"/api/analyses/{analysis_id}").json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("analysis did not finish")


def test_recording_round_trips_through_upload(rec_client, sample_csv, tmp_path):
    packets = csv_packets(sample_csv)
    sid = _record(rec_client, packets)

    during = rec_client.get(f"/api/sessions/{sid}/recording").json()
    assert during["status"] == "recording" and during["downloadable"] is False
    assert during["packets"] == len(packets)
    blocked = rec_client.get(f"/api/sessions/{sid}/recording/csv")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["detail"]["status"] == "recording"

    live_events = rec_client.get(f"/api/sessions/{sid}").json()["event_count"]
    _stop(rec_client, sid)
    ready = rec_client.get(f"/api/sessions/{sid}/recording").json()
    assert ready["status"] == "ready" and ready["downloadable"] is True
    assert ready["expires_at"] == pytest.approx(rec_client.clock.now + 300)
    assert ready["duration_sec"] == pytest.approx(packets[-1]["ts"] - packets[0]["ts"], abs=1e-3)

    res = rec_client.get(f"/api/sessions/{sid}/recording/csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert res.headers["content-disposition"].startswith('attachment; filename="csi_raw_live_')
    assert len(res.content) == ready["size_bytes"]

    path = tmp_path / "recorded.csv"
    path.write_bytes(res.content)
    table = load_csi_csv(path)
    original = load_csi_csv(sample_csv)
    assert table.preview.packets == len(packets)
    assert list(table.rx) == list(original.rx)
    assert np.allclose(table.amplitude, original.amplitude, atol=1e-6)
    assert table.origin_ts == pytest.approx(original.origin_ts, abs=1e-5)
    assert np.allclose(table.offset, original.offset, atol=1e-5)

    upload = rec_client.post("/api/files", files={"file": ("recorded.csv", io.BytesIO(res.content), "text/csv")})
    assert upload.status_code == 201, upload.text
    assert rec_client.get("/api/files").json()[0]["id"] == upload.json()["id"]
    started = rec_client.post(f"/api/files/{upload.json()['id']}/analyses").json()
    analysis = _wait(rec_client, started["id"])
    assert analysis["status"] == "succeeded"
    assert analysis["event_count"] == live_events

    with open(sample_csv, "rb") as f:
        source_id = rec_client.post("/api/files", files={"file": ("source.csv", f, "text/csv")}).json()["id"]
    reference = _wait(rec_client, rec_client.post(f"/api/files/{source_id}/analyses").json()["id"])
    params = {"max_points": 7200}
    got = rec_client.get(f"/api/analyses/{analysis['id']}/signals", params=params).json()
    want = rec_client.get(f"/api/analyses/{reference['id']}/signals", params=params).json()
    assert got["count"] == want["count"] > 0
    for key in ("state", "activity_score", "motion_index"):
        assert got[key] == want[key]
    assert analysis["event_count"] == reference["event_count"]


def test_new_session_deletes_previous_recording(rec_client, sample_csv):
    packets = csv_packets(sample_csv, 600)
    first = _record(rec_client, packets)
    _stop(rec_client, first)
    old_path = rec_client.app.state.sessions.recorder.current.path
    assert old_path.exists()

    second = start_session(rec_client)
    assert not old_path.exists()
    assert rec_client.get(f"/api/sessions/{first}/recording").status_code == 404
    assert rec_client.get(f"/api/sessions/{first}/recording/csv").status_code == 404
    assert rec_client.get(f"/api/sessions/{second}/recording").json()["status"] == "recording"


def test_recording_expires_after_keep_time(rec_client, sample_csv):
    sid = _record(rec_client, csv_packets(sample_csv, 600))
    _stop(rec_client, sid)
    path = rec_client.app.state.sessions.recorder.current.path
    stopped = rec_client.clock.now

    rec_client.clock.now = stopped + 299
    assert rec_client.get(f"/api/sessions/{sid}/recording").json()["status"] == "ready"
    rec_client.clock.now = stopped + 300
    rec_client.app.state.sessions.tick()
    assert not path.exists()
    body = rec_client.get(f"/api/sessions/{sid}/recording").json()
    assert body["status"] == "expired" and body["downloadable"] is False
    res = rec_client.get(f"/api/sessions/{sid}/recording/csv")
    assert res.status_code == 409
    assert res.json()["error"] == {"code": "recording_unavailable", "message": res.json()["error"]["message"],
                                   "detail": {"session_id": sid, "status": "expired"}}


def test_too_long_recording_is_dropped(live_settings, sample_csv):
    client = _client(replace(live_settings, recording_max_sec=30.0))
    try:
        sid = _record(client, csv_packets(sample_csv))
        recorder = client.app.state.sessions.recorder
        assert not recorder.current.path.exists()
        assert client.get(f"/api/sessions/{sid}/recording").json()["status"] == "too_long"
        _stop(client, sid)
        body = client.get(f"/api/sessions/{sid}/recording").json()
        assert body["status"] == "too_long" and body["downloadable"] is False
        assert client.get(f"/api/sessions/{sid}/recording/csv").status_code == 409
    finally:
        client.__exit__(None, None, None)


def test_stop_without_packets_is_empty(rec_client):
    sid = start_session(rec_client)
    _stop(rec_client, sid)
    assert rec_client.get(f"/api/sessions/{sid}/recording").json()["status"] == "empty"


def test_restart_clears_leftover_recordings(live_settings):
    folder = live_settings.recordings_path
    folder.mkdir(parents=True)
    (folder / "old.csv").write_text("x", encoding="utf-8")
    client = _client(live_settings)
    try:
        assert not (folder / "old.csv").exists()
    finally:
        client.__exit__(None, None, None)


def test_recording_of_unknown_session_is_404(rec_client):
    assert rec_client.get("/api/sessions/00000000-0000-0000-0000-000000000000/recording").status_code == 404
    assert rec_client.get("/api/sessions/00000000-0000-0000-0000-000000000000/recording/csv").status_code == 404
