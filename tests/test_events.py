from __future__ import annotations

import pytest
from helpers import FakeClock, csv_packets, send_packets, start_session

from app.config import Settings, load_settings
from app.repositories import memory_repositories, supabase_repositories
from app.services.events import EventNotFound, EventService
from app.services.monitoring import SessionManager


def _session_with_events(app_client, sample_csv) -> str:
    sid = start_session(app_client)
    send_packets(app_client, sid, csv_packets(sample_csv))
    return sid


def test_list_counts_and_filters(app_client, sample_csv):
    sid = _session_with_events(app_client, sample_csv)
    body = app_client.get("/api/events", params={"session_id": sid}).json()
    assert body["counts"] == {"all": 2, "unconfirmed": 2, "confirmed": 0}
    assert body["total"] == 2
    first, second = body["items"]
    assert (first["number"], second["number"]) == (1, 2)
    assert first["started_at"] < second["started_at"]
    assert first["status"] == "unconfirmed"
    assert first["status_ko"] == "보호자 확인 대기"
    assert first["title"] == "지속 활동 감지"
    assert first["source"] == "live"
    assert first["alert_message"].startswith("취침 모드 중 움직임이")
    assert first["ended_at"] is not None and first["duration_sec"] > 3

    newest = app_client.get("/api/events", params={"session_id": sid, "order": "desc", "limit": 1}).json()
    assert [e["number"] for e in newest["items"]] == [2]
    assert newest["total"] == 2

    app_client.patch(f"/api/events/{first['id']}/confirmation", json={"result": "정상 활동"})
    confirmed = app_client.get("/api/events", params={"session_id": sid, "status": "confirmed"}).json()
    assert confirmed["counts"] == {"all": 2, "unconfirmed": 1, "confirmed": 1}
    assert [e["number"] for e in confirmed["items"]] == [1]
    pending = app_client.get("/api/events", params={"session_id": sid, "status": "unconfirmed"}).json()
    assert [e["number"] for e in pending["items"]] == [2]


def test_confirmation_flow(app_client, sample_csv):
    sid = _session_with_events(app_client, sample_csv)
    status = app_client.get(f"/api/sessions/{sid}").json()
    assert status["unconfirmed_count"] == 2
    assert status["state"] == "awaiting_confirmation"

    events = app_client.get("/api/events", params={"session_id": sid}).json()["items"]
    for event, result in zip(events, ["도움 필요", "잘못된 감지"]):
        res = app_client.patch(f"/api/events/{event['id']}/confirmation", json={"result": result})
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "confirmed"
        assert body["guardian_result"] == result
        assert body["status_ko"] == f"{result} 확인 완료"
        assert body["confirmed_at"] == pytest.approx(app_client.clock.now, abs=1e-3)

    status = app_client.get(f"/api/sessions/{sid}").json()
    assert status["unconfirmed_count"] == 0
    assert status["state"] != "awaiting_confirmation"
    live = app_client.app.state.sessions.live[sid]
    assert live.engine.open_events() == []

    app_client.clock.now += 10
    again = app_client.patch(f"/api/events/{events[0]['id']}/confirmation", json={"result": "정상 활동"}).json()
    assert again["guardian_result"] == "정상 활동"
    assert again["confirmed_at"] == pytest.approx(app_client.clock.now, abs=1e-3)
    assert len(app_client.app.state.repos.guardian_confirmations.list()) == 2

    detail = app_client.get(f"/api/events/{events[0]['id']}").json()
    assert detail["guardian_result"] == "정상 활동"
    markers = app_client.get(f"/api/sessions/{sid}/signals", params={"window": 3600}).json()["events"]
    assert [m["guardian_result"] for m in markers] == ["정상 활동", "잘못된 감지"]
    assert [m["uuid"] for m in markers] == [e["id"] for e in events]


def test_confirmation_validation_and_missing(app_client, sample_csv):
    sid = _session_with_events(app_client, sample_csv)
    event_id = app_client.get("/api/events", params={"session_id": sid}).json()["items"][0]["id"]
    for bad in ({"result": "확인 중"}, {"result": "normal"}, {}):
        res = app_client.patch(f"/api/events/{event_id}/confirmation", json=bad)
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "invalid_request"
    assert app_client.get(f"/api/events/{event_id}").json()["status"] == "unconfirmed"

    missing = app_client.get("/api/events/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "event_not_found"
    assert app_client.patch("/api/events/nope/confirmation", json={"result": "정상 활동"}).status_code == 404


def test_alert_carries_event_id_saved_immediately(app_client, sample_csv):
    sid = start_session(app_client)
    packets = csv_packets(sample_csv)
    with app_client.websocket_connect(f"/ws/sessions/{sid}") as ws:
        ws.receive_json()
        send_packets(app_client, sid, packets[:900])
        alert = None
        while alert is None:
            msg = ws.receive_json()
            if msg["type"] == "alert":
                alert = msg
        stored = app_client.get(f"/api/events/{alert['event_uuid']}")
        assert stored.status_code == 200
        assert stored.json()["number"] == alert["event_id"] == 1
        assert stored.json()["ongoing"] is True

        app_client.patch(f"/api/events/{alert['event_uuid']}/confirmation", json={"result": "정상 활동"})
        seen = None
        while seen is None:
            msg = ws.receive_json()
            if msg["type"] == "event" and msg["id"] == alert["event_uuid"]:
                seen = msg
        assert seen["number"] == 1


def test_events_survive_restart_and_stop(live_settings, sample_csv):
    repos = memory_repositories(live_settings.local_storage_dir)
    clock = FakeClock()
    first = SessionManager(live_settings, repos, "sha", clock)
    row, _ = first.start()
    packets = csv_packets(sample_csv)
    clock.now = packets[1500]["ts"]
    first.ingest(row["id"], packets[:1500])
    first.tick()
    before = EventService(repos, first, clock).list(session_id=row["id"])
    assert before["counts"]["all"] == 1

    second = SessionManager(live_settings, repos, "sha", clock)
    service = EventService(repos, second, clock)
    event_id = before["items"][0]["id"]
    service.confirm(event_id, "정상 활동")
    status = second.status(row["id"])
    assert status["unconfirmed_count"] == 0

    clock.now = packets[-1]["ts"]
    second.ingest(row["id"], packets[1500:])
    second.stop(row["id"])
    after = service.list(session_id=row["id"])
    assert [e["number"] for e in after["items"]] == sorted({e["number"] for e in after["items"]})
    assert after["items"][0]["id"] == event_id
    assert after["items"][0]["guardian_result"] == "정상 활동"
    assert all(not e["ongoing"] for e in after["items"])


def test_old_unconfirmed_event_keeps_awaiting_state_after_restart(live_settings, sample_csv):
    repos = memory_repositories(live_settings.local_storage_dir)
    clock = FakeClock()
    first = SessionManager(live_settings, repos, "sha", clock)
    row, _ = first.start()
    packets = csv_packets(sample_csv)
    clock.now = packets[1500]["ts"]
    first.ingest(row["id"], packets[:1500])
    first.tick()

    second = SessionManager(live_settings, repos, "sha", clock)
    later = [{**p, "ts": p["ts"] + 30} for p in packets[1500:2400]]
    clock.now = later[-1]["ts"]
    second.ingest(row["id"], later)
    status = second.status(row["id"])
    assert status["unconfirmed_count"] >= 1
    assert status["state"] != "low_motion"


def test_list_without_filters_includes_all_sessions(app_client, sample_csv):
    sid = _session_with_events(app_client, sample_csv)
    app_client.post(f"/api/sessions/{sid}/stop")
    other = start_session(app_client)
    shifted = [{**p, "ts": p["ts"] + 3600} for p in csv_packets(sample_csv)]
    send_packets(app_client, other, shifted)
    everything = app_client.get("/api/events").json()
    assert everything["counts"]["all"] == 4
    assert {e["session_id"] for e in everything["items"]} == {sid, other}


@pytest.mark.supabase
def test_supabase_confirmation_roundtrip(tmp_path, sample_csv):
    s = load_settings()
    if not (s.supabase_url and s.supabase_key):
        pytest.skip("Supabase 설정 없음")
    repos = supabase_repositories(s.supabase_url, s.supabase_key, s.csv_bucket, s.results_bucket)
    if repos.sessions.list({"status": "running"}):
        pytest.skip("실제 실행 중인 세션이 있어 건너뜀")
    settings = Settings(storage_backend="supabase", local_storage_dir=tmp_path, tick_sec=3600)
    clock = FakeClock()
    manager = SessionManager(settings, repos, "pytest", clock)
    service = EventService(repos, manager, clock)
    row, _ = manager.start()
    try:
        packets = csv_packets(sample_csv, 1500)
        clock.now = packets[-1]["ts"]
        manager.ingest(row["id"], packets)
        listed = service.list(session_id=row["id"])
        assert listed["counts"]["all"] == 1
        event_id = listed["items"][0]["id"]
        assert service.confirm(event_id, "도움 필요")["guardian_result"] == "도움 필요"
        assert service.confirm(event_id, "정상 활동")["guardian_result"] == "정상 활동"
        assert len(repos.guardian_confirmations.list({"event_id": event_id})) == 1
        with pytest.raises(EventNotFound):
            service.get("not-a-uuid")
        manager.stop(row["id"])
        assert service.get(event_id)["status_ko"] == "정상 활동 확인 완료"
    finally:
        repos.sessions.delete(row["id"])
    assert repos.activity_events.list({"session_id": row["id"]}) == []
    assert repos.guardian_confirmations.list({"event_id": event_id}) == []
