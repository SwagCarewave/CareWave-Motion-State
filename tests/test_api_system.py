from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import FINAL_MODEL_SHA256
from app.services.model_guard import ModelIntegrityError


def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["model"] == "lying_walking_v2"
    assert body["storage"] == "memory"
    assert body["checks"] == {"database": "ok", "storage": "ok"}


def test_model(client):
    res = client.get("/api/model")
    assert res.status_code == 200
    body = res.json()
    assert body["sha256"] == FINAL_MODEL_SHA256
    assert body["feature_count"] == 60


def test_unknown_route_uses_error_shape(client):
    res = client.get("/api/nope")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "http_404"


def test_startup_refuses_changed_model(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    bad = replace(settings, model_sha256="0" * 64)
    with pytest.raises(ModelIntegrityError):
        with TestClient(create_app(bad)):
            pass



def test_health_degraded_when_backend_rejects(client):
    import httpx

    from app.repositories import supabase_repositories

    def handler(request):
        return httpx.Response(401, json={"message": "Invalid API key"})

    client.app.state.repos = supabase_repositories("https://example.supabase.co", "k", "a", "b",
                                                   transport=httpx.MockTransport(handler))
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "degraded"
    assert res.json()["checks"] == {"database": "error", "storage": "error"}
    assert "Invalid API key" not in res.text


def test_codes_match_engine_constants(client):
    from typing import get_args

    from app.schemas.codes import (EVENT_FILTER_KO, EVENT_STATUS_KO, STREAM_MESSAGE_RULE, EventFilter, EventStatus,
                                   GuardianResult, MonitorState, RxId, RxStatus, StreamMessage)
    from app.services.engine import FINAL_GUARDIAN_RESULTS, RX_STATUS_KO
    from motion_state.csi_io import RX_IDS
    from motion_state.monitor_v2 import STATE_KO

    assert get_args(MonitorState) == tuple(STATE_KO)
    assert get_args(RxStatus) == tuple(RX_STATUS_KO)
    assert get_args(RxId) == tuple(RX_IDS)
    assert get_args(GuardianResult) == FINAL_GUARDIAN_RESULTS
    assert get_args(EventStatus) == tuple(EVENT_STATUS_KO)
    assert get_args(EventFilter) == tuple(EVENT_FILTER_KO)
    assert get_args(StreamMessage) == tuple(STREAM_MESSAGE_RULE)

    body = client.get("/api/codes").json()
    assert [s["code"] for s in body["state"]] == list(STATE_KO)
    assert body["guardian_result"] == list(FINAL_GUARDIAN_RESULTS)
    assert {e["code"] for e in body["error"]} >= {"session_not_found", "session_stopped", "invalid_request"}

    schema = client.get("/openapi.json").json()["components"]["schemas"]
    assert schema["RxStatusOut"]["properties"]["status"]["enum"] == list(RX_STATUS_KO)
