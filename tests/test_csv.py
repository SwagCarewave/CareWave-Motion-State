from __future__ import annotations

import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import FakeClock

from app.config import Settings, load_settings
from app.main import create_app
from app.repositories import BackendUnavailable, memory_repositories, supabase_repositories
from app.services.csv_reader import load_csi_csv
from app.services.engine import build_engine, run_table
from motion_state.csi_io import SUB_COLS


@pytest.fixture
def csv_settings(tmp_path):
    return Settings(storage_backend="memory", local_storage_dir=tmp_path / "storage", tick_sec=3600,
                    replay_tick_sec=3600)


@pytest.fixture
def csv_client(csv_settings):
    with TestClient(create_app(csv_settings)) as c:
        c.clock = FakeClock()
        c.app.state.replays.clock = c.clock
        yield c


def _upload(client, path, name="night_csi_20261006.csv"):
    with open(path, "rb") as f:
        return client.post("/api/files", files={"file": (name, f, "text/csv")})


def _wait(client, analysis_id, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        body = client.get(f"/api/analyses/{analysis_id}").json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("analysis did not finish")


def _analyzed(client, sample_csv):
    file_id = _upload(client, sample_csv).json()["id"]
    analysis = client.post(f"/api/files/{file_id}/analyses").json()
    return file_id, _wait(client, analysis["id"])


def test_upload_preview_and_list(csv_client, sample_csv):
    assert csv_client.get("/api/files").json() == []
    res = _upload(csv_client, sample_csv)
    assert res.status_code == 201
    body = res.json()
    preview = load_csi_csv(sample_csv).preview
    assert body["filename"] == "night_csi_20261006.csv"
    assert body["receivers"] == ["RX1", "RX2", "RX3"]
    assert body["packets"] == preview.packets
    assert body["incomplete_rows"] == preview.incomplete_rows
    assert body["record_start"] == pytest.approx(preview.start_ts, abs=1e-3)
    assert body["duration_sec"] == pytest.approx(preview.duration_sec, abs=1e-2)
    assert body["latest_analysis"] is None
    listed = csv_client.get("/api/files").json()
    assert [f["id"] for f in listed] == [body["id"]]
    assert csv_client.get(f"/api/files/{body['id']}").json()["size_bytes"] == sample_csv.stat().st_size
    stored = csv_client.app.state.repos.csv_store.get(f"{body['id']}.csv")
    assert stored == sample_csv.read_bytes()


@pytest.mark.parametrize("name,content,code,status", [
    ("notes.txt", b"timestamp,rx\n", "unsupported_file", 422),
    ("empty.csv", b"", "empty_file", 422),
    ("bad.csv", b"timestamp,value\n2026-10-06T22:00:00+09:00,1\n", "missing_columns", 422),
    ("garbage.csv", b"\xff\xfe\x00\x01", None, 422),
])
def test_upload_rejects_invalid_files(csv_client, tmp_path, name, content, code, status):
    path = tmp_path / name
    path.write_bytes(content)
    res = _upload(csv_client, path, name)
    assert res.status_code == status
    if code:
        assert res.json()["error"]["code"] == code
    assert csv_client.get("/api/files").json() == []
    assert csv_client.app.state.repos.csv_files.list() == []


def test_upload_size_limit(tmp_path, sample_csv):
    settings = Settings(storage_backend="memory", local_storage_dir=tmp_path / "s", tick_sec=3600, max_upload_mb=1)
    with TestClient(create_app(settings)) as c:
        res = _upload(c, sample_csv)
        assert res.status_code == 413
        assert res.json()["error"]["code"] == "file_too_large"
        assert c.get("/api/files").json() == []


def test_storage_failure_does_not_register_file(csv_client, sample_csv):
    class DownStore:
        bucket = "csv-uploads"

        def put(self, *args, **kwargs):
            raise BackendUnavailable("storage", "storage down")

    repos = csv_client.app.state.repos
    csv_client.app.state.files.repos = replace(repos, csv_store=DownStore())
    res = _upload(csv_client, sample_csv)
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "storage_unavailable"
    assert csv_client.get("/api/files").json() == []
    assert [r["status"] for r in repos.csv_files.list()] == ["upload_failed"]


def test_analysis_matches_engine_and_feeds_events(csv_client, sample_csv):
    file_id, analysis = _analyzed(csv_client, sample_csv)
    assert analysis["status"] == "succeeded"
    assert analysis["progress"] == 1.0

    table = load_csi_csv(sample_csv)
    engine = build_engine(Settings(), origin_ts=table.origin_ts)
    reference = run_table(engine, table)
    assert analysis["frame_count"] == len(reference)
    assert analysis["event_count"] == len(engine.events()) == 2

    signals = csv_client.get(f"/api/analyses/{analysis['id']}/signals", params={"max_points": 7200}).json()
    assert signals["count"] == len(reference)
    assert signals["state"] == [f.state for f in reference]
    assert signals["activity_score"] == [f.activity_score for f in reference]
    assert [(e["id"], e["duration_sec"]) for e in signals["events"]] == [(e["id"], e["duration_sec"])
                                                                         for e in engine.events()]
    small = csv_client.get(f"/api/analyses/{analysis['id']}/signals", params={"max_points": 50}).json()
    assert small["count"] <= 50 and small["step_sec"] > 0.5

    events = csv_client.get("/api/events", params={"analysis_id": analysis["id"]}).json()
    assert events["counts"] == {"all": 2, "unconfirmed": 2, "confirmed": 0}
    assert {e["source"] for e in events["items"]} == {"csv"}
    first = events["items"][0]
    confirmed = csv_client.patch(f"/api/events/{first['id']}/confirmation", json={"result": "잘못된 감지"}).json()
    assert confirmed["status_ko"] == "잘못된 감지 확인 완료"

    listed = csv_client.get("/api/files").json()[0]["latest_analysis"]
    assert listed["status"] == "succeeded" and listed["event_count"] == 2


def test_start_is_idempotent_while_running(csv_client, sample_csv):
    file_id = _upload(csv_client, sample_csv).json()["id"]
    first = csv_client.post(f"/api/files/{file_id}/analyses")
    second = csv_client.post(f"/api/files/{file_id}/analyses")
    assert first.status_code == 201
    if second.status_code == 200:
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["created"] is False
    _wait(csv_client, first.json()["id"])


def test_signals_before_ready_and_missing(csv_client, sample_csv):
    file_id = _upload(csv_client, sample_csv).json()["id"]
    repos = csv_client.app.state.repos
    row = repos.analyses.insert({"file_id": file_id, "status": "running", "progress": 0.3, "attempt": 1})
    res = csv_client.get(f"/api/analyses/{row['id']}/signals")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "analysis_not_ready"
    assert csv_client.get("/api/analyses/nope").json()["error"]["code"] == "analysis_not_found"
    assert csv_client.post("/api/files/nope/analyses").json()["error"]["code"] == "file_not_found"


def test_failed_analysis_can_be_retried(csv_client, sample_csv):
    file_id = _upload(csv_client, sample_csv).json()["id"]
    store = csv_client.app.state.repos.csv_store
    store.put(f"{file_id}.csv", b"timestamp,value\n2026-10-06T22:00:00+09:00,1\n", "text/csv")
    failed = _wait(csv_client, csv_client.post(f"/api/files/{file_id}/analyses").json()["id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "missing_columns"
    assert failed["error_message"]

    store.put(f"{file_id}.csv", sample_csv.read_bytes(), "text/csv")
    retried = csv_client.post(f"/api/files/{file_id}/analyses").json()
    assert retried["attempt"] == 2
    assert _wait(csv_client, retried["id"])["status"] == "succeeded"

    store.delete([f"{file_id}.csv"])
    missing = _wait(csv_client, csv_client.post(f"/api/files/{file_id}/analyses").json()["id"])
    assert missing["error_code"] == "source_missing"


def test_reanalysis_keeps_confirmations_and_replaces_old(csv_client, sample_csv):
    file_id, first = _analyzed(csv_client, sample_csv)
    events = csv_client.get("/api/events", params={"analysis_id": first["id"]}).json()["items"]
    csv_client.patch(f"/api/events/{events[1]['id']}/confirmation", json={"result": "도움 필요"})

    second = _wait(csv_client, csv_client.post(f"/api/files/{file_id}/analyses").json()["id"])
    assert second["status"] == "succeeded"
    assert csv_client.get(f"/api/analyses/{first['id']}").status_code == 404
    new_events = csv_client.get("/api/events", params={"analysis_id": second["id"]}).json()
    assert [e["guardian_result"] for e in new_events["items"]] == [None, "도움 필요"]
    assert csv_client.get("/api/events").json()["counts"]["all"] == 2
    assert len(csv_client.app.state.repos.guardian_confirmations.list()) == 1


def test_interrupted_analysis_is_marked_failed(csv_settings, sample_csv):
    repos = memory_repositories(csv_settings.local_storage_dir)
    from app.services.analysis import AnalysisService
    from app.services.files import FileService

    files = FileService(csv_settings, repos)
    with open(sample_csv, "rb") as f:
        file_id = files.upload("a.csv", f)["id"]
    stuck = repos.analyses.insert({"file_id": file_id, "status": "running", "progress": 0.5, "attempt": 1})
    AnalysisService(csv_settings, repos, files).recover()
    row = repos.analyses.get(stuck["id"])
    assert row["status"] == "failed"
    assert row["error_code"] == "interrupted"


def test_replay_controls(csv_client, sample_csv):
    _, analysis = _analyzed(csv_client, sample_csv)
    clock = csv_client.clock
    created = csv_client.post("/api/replays", json={"analysis_id": analysis["id"]}).json()
    assert created["status"] == "paused"
    assert created["position_ts"] == created["from_ts"] == created["record_start"]
    rid = created["id"]

    playing = csv_client.patch(f"/api/replays/{rid}", json={"action": "play", "speed": 4}).json()
    assert playing["status"] == "playing"
    clock.now += 5
    state = csv_client.get(f"/api/replays/{rid}").json()
    assert state["position_ts"] == pytest.approx(created["from_ts"] + 20, abs=1e-3)
    csv_client.patch(f"/api/replays/{rid}", json={"speed": 16})
    clock.now += 1
    assert csv_client.get(f"/api/replays/{rid}").json()["position_ts"] == pytest.approx(created["from_ts"] + 36, abs=1e-3)

    paused = csv_client.patch(f"/api/replays/{rid}", json={"action": "pause"}).json()
    clock.now += 100
    assert csv_client.get(f"/api/replays/{rid}").json()["position_ts"] == paused["position_ts"]

    seek = csv_client.patch(f"/api/replays/{rid}", json={"position_ts": created["to_ts"] + 999}).json()
    assert seek["position_ts"] == created["to_ts"]
    assert seek["status"] == "ended"
    restart = csv_client.patch(f"/api/replays/{rid}", json={"action": "play"}).json()
    assert restart["position_ts"] == created["from_ts"]

    ranged = csv_client.patch(f"/api/replays/{rid}", json={"from_ts": created["from_ts"] + 30,
                                                           "to_ts": created["from_ts"] + 60}).json()
    assert ranged["from_ts"] == pytest.approx(created["from_ts"] + 30, abs=1e-3)
    clock.now += 100
    assert csv_client.get(f"/api/replays/{rid}").json()["status"] == "ended"

    assert csv_client.patch(f"/api/replays/{rid}", json={"speed": 2}).status_code == 422
    bad_range = csv_client.patch(f"/api/replays/{rid}", json={"from_ts": created["to_ts"], "to_ts": created["from_ts"]})
    assert bad_range.json()["error"]["code"] == "invalid_range"
    assert csv_client.get("/api/replays/nope").json()["error"]["code"] == "replay_not_found"


def test_replay_requires_finished_analysis(csv_client, sample_csv):
    file_id = _upload(csv_client, sample_csv).json()["id"]
    row = csv_client.app.state.repos.analyses.insert({"file_id": file_id, "status": "queued", "attempt": 1})
    res = csv_client.post("/api/replays", json={"analysis_id": row["id"]})
    assert res.status_code == 409


def test_replay_websocket_streams_frames(csv_client, sample_csv):
    _, analysis = _analyzed(csv_client, sample_csv)
    replays = csv_client.app.state.replays
    rid = csv_client.post("/api/replays", json={"analysis_id": analysis["id"], "speed": 16}).json()["id"]
    with csv_client.websocket_connect(f"/ws/replays/{rid}") as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["replay"]["status"] == "paused"
        csv_client.patch(f"/api/replays/{rid}", json={"action": "play"})
        assert ws.receive_json()["type"] == "state"
        replays.tick()
        csv_client.clock.now += 0.5
        replays.tick()
        tick = ws.receive_json()
        while tick["type"] != "tick" or not tick["frames"]:
            tick = ws.receive_json()
        assert len(tick["frames"]) == 16
        assert tick["frames"][-1]["ts"] <= tick["replay"]["position_ts"]
        assert len(tick["frames"][0]["heatmap"]) == 52


def test_delete_cleans_everything(csv_client, sample_csv):
    file_id, analysis = _analyzed(csv_client, sample_csv)
    repos = csv_client.app.state.repos
    event_id = csv_client.get("/api/events", params={"analysis_id": analysis["id"]}).json()["items"][0]["id"]
    csv_client.patch(f"/api/events/{event_id}/confirmation", json={"result": "정상 활동"})
    rid = csv_client.post("/api/replays", json={"analysis_id": analysis["id"]}).json()["id"]

    assert csv_client.delete(f"/api/files/{file_id}").status_code == 204
    assert csv_client.get("/api/files").json() == []
    assert csv_client.get(f"/api/files/{file_id}").status_code == 404
    assert csv_client.get(f"/api/analyses/{analysis['id']}").status_code == 404
    assert csv_client.get(f"/api/replays/{rid}").status_code == 404
    assert csv_client.get("/api/events").json()["counts"]["all"] == 0
    assert repos.guardian_confirmations.list() == []
    assert not repos.csv_store.exists(f"{file_id}.csv")
    assert not repos.results_store.exists(f"{analysis['id']}.json.gz")
    assert csv_client.delete(f"/api/files/{file_id}").status_code == 404


def test_delete_stops_running_analysis(csv_client, sample_csv, tmp_path):
    big = tmp_path / "long.csv"
    lines = sample_csv.read_text(encoding="utf-8").splitlines()
    big.write_text("\n".join(lines * 3) + "\n", encoding="utf-8")
    file_id = _upload(csv_client, big).json()["id"]
    analysis_id = csv_client.post(f"/api/files/{file_id}/analyses").json()["id"]
    assert csv_client.delete(f"/api/files/{file_id}").status_code == 204
    time.sleep(1.5)
    repos = csv_client.app.state.repos
    assert repos.analyses.get(analysis_id) is None
    assert repos.activity_events.list({"analysis_id": analysis_id}) == []
    assert not repos.results_store.exists(f"{analysis_id}.json.gz")


def test_codes_include_csv_values(csv_client):
    body = csv_client.get("/api/codes").json()
    assert [s["code"] for s in body["analysis_status"]] == ["queued", "running", "succeeded", "failed"]
    assert [s["value"] for s in body["replay_speed"]] == [1, 4, 16]
    assert {e["code"] for e in body["error"]} >= {"file_too_large", "analysis_not_ready", "replay_not_found"}


def test_missing_subcarrier_header_message(csv_client, tmp_path):
    path = tmp_path / "partial.csv"
    header = ",".join(["timestamp", "rx", *SUB_COLS[:10]])
    path.write_text(header + "\n", encoding="utf-8")
    res = _upload(csv_client, path, "partial.csv")
    assert res.json()["error"]["code"] == "missing_columns"
    assert "sub_10" in res.json()["error"]["detail"]["missing"]


@pytest.mark.supabase
def test_supabase_csv_roundtrip(tmp_path, sample_csv):
    s = load_settings()
    if not (s.supabase_url and s.supabase_key):
        pytest.skip("Supabase 설정 없음")
    settings = Settings(storage_backend="supabase", supabase_url=s.supabase_url, supabase_key=s.supabase_key,
                        local_storage_dir=tmp_path, tick_sec=3600, replay_tick_sec=3600)
    with TestClient(create_app(settings)) as c:
        file_id = _upload(c, sample_csv, "pytest_roundtrip.csv").json()["id"]
        try:
            analysis = _wait(c, c.post(f"/api/files/{file_id}/analyses").json()["id"], timeout=120)
            assert analysis["status"] == "succeeded"
            assert analysis["event_count"] == 2
            signals = c.get(f"/api/analyses/{analysis['id']}/signals").json()
            assert signals["count"] > 0 and len(signals["events"]) == 2
            c.app.state.analyses.cache.clear()
            again = c.get(f"/api/analyses/{analysis['id']}/signals").json()
            assert again["count"] == signals["count"]
        finally:
            c.delete(f"/api/files/{file_id}")
        repos = supabase_repositories(s.supabase_url, s.supabase_key, s.csv_bucket, s.results_bucket)
        assert repos.csv_files.get(file_id) is None
        assert not repos.csv_store.exists(f"{file_id}.csv")
        assert not repos.results_store.exists(f"{analysis['id']}.json.gz")
        assert repos.activity_events.list({"analysis_id": analysis["id"]}) == []


def test_event_labels_follow_duration(tmp_path, sample_csv):
    settings = Settings(storage_backend="memory", local_storage_dir=tmp_path / "s", tick_sec=3600,
                        replay_tick_sec=3600, event_burst_sec=20.0)
    with TestClient(create_app(settings)) as c:
        _, analysis = _analyzed(c, sample_csv)
        markers = c.get(f"/api/analyses/{analysis['id']}/signals").json()["events"]
        assert [(m["duration_sec"], m["label"]) for m in markers] == [(16.5, "짧은 움직임"), (21.5, "움직임 급증")]
        items = c.get("/api/events", params={"analysis_id": analysis["id"]}).json()["items"]
        assert [e["label"] for e in items] == ["짧은 움직임", "움직임 급증"]
