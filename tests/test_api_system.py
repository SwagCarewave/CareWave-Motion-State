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

