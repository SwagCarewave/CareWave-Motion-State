from __future__ import annotations

import uuid

import httpx
import pytest

from app.config import load_settings
from app.repositories import (BackendUnavailable, ObjectNotFound, RepositoryError, memory_repositories,
                              supabase_repositories)
from app.repositories.supabase import to_params


def _supabase():
    s = load_settings()
    if not (s.supabase_url and s.supabase_key):
        pytest.skip("Supabase 설정 없음")
    return supabase_repositories(s.supabase_url, s.supabase_key, s.csv_bucket, s.results_bucket)


@pytest.fixture(params=["memory", pytest.param("supabase", marks=pytest.mark.supabase)])
def repos(request, tmp_path):
    tag = f"pytest-{uuid.uuid4().hex[:8]}"
    if request.param == "memory":
        yield memory_repositories(tmp_path), tag
        return
    r = _supabase()
    yield r, tag
    r.csv_files.delete_where({"filename__in": [f"{tag}.csv", f"{tag}-b.csv"]})
    r.sessions.delete_where({"model_sha256": tag})
    r.csv_store.delete([f"{tag}/a.csv"])


def _file_row(tag: str, name: str) -> dict:
    return {
        "filename": name,
        "storage_path": f"{tag}/{name}",
        "size_bytes": 10,
        "sha256": "0" * 64,
        "rows": 1,
        "packets": 1,
        "receivers": ["RX1"],
        "packets_by_rx": {"RX1": 1},
        "record_start": "2026-10-06T13:00:00+00:00",
        "record_end": "2026-10-06T23:00:00+00:00",
    }


def test_table_contract(repos):
    repos, tag = repos
    a = repos.csv_files.insert(_file_row(tag, f"{tag}.csv"))
    b = repos.csv_files.insert(_file_row(tag, f"{tag}-b.csv"))
    assert repos.csv_files.get(a["id"])["filename"] == f"{tag}.csv"
    assert repos.csv_files.get(str(uuid.uuid4())) is None

    updated = repos.csv_files.update(a["id"], {"status": "uploaded"})
    assert updated["status"] == "uploaded"
    assert repos.csv_files.update(str(uuid.uuid4()), {"status": "uploaded"}) is None

    names = {f"{tag}.csv", f"{tag}-b.csv"}
    listed = repos.csv_files.list({"filename__in": sorted(names)}, order_by="filename")
    assert [r["filename"] for r in listed] == sorted(names)
    assert [r["id"] for r in repos.csv_files.list({"filename__in": sorted(names), "status": "uploaded"})] == [a["id"]]
    assert len(repos.csv_files.list({"filename__in": sorted(names)}, limit=1)) == 1

    again = repos.csv_files.insert_many([{**_file_row(tag, f"{tag}-b.csv"), "id": b["id"]}])
    assert [r["id"] for r in again] == [b["id"]]
    assert len(repos.csv_files.list({"filename__in": sorted(names)})) == 2

    assert repos.csv_files.delete(a["id"])
    assert not repos.csv_files.delete(a["id"])
    assert repos.csv_files.delete_where({"filename__in": sorted(names)}) == 1


def test_upsert_and_cascade(repos):
    repos, tag = repos
    session = repos.sessions.insert({"model_sha256": tag})
    first = repos.device_status.upsert({"session_id": session["id"], "rx": "RX1", "status": "ok", "packet_rate": 10},
                                       ("session_id", "rx"))
    second = repos.device_status.upsert({"session_id": session["id"], "rx": "RX1", "status": "weak",
                                         "packet_rate": 1}, ("session_id", "rx"))
    assert first["id"] == second["id"]
    assert second["status"] == "weak"
    assert len(repos.device_status.list({"session_id": session["id"]})) == 1
    assert repos.sessions.delete(session["id"])
    if repos.backend == "supabase":
        assert repos.device_status.list({"session_id": session["id"]}) == []


def test_object_store_contract(repos):
    repos, tag = repos
    path = f"{tag}/a.csv"
    assert not repos.csv_store.exists(path)
    repos.csv_store.put(path, b"timestamp,rx\n", "text/csv")
    assert repos.csv_store.exists(path)
    assert repos.csv_store.get(path) == b"timestamp,rx\n"
    repos.csv_store.put(path, b"timestamp,rx,sub_0\n", "text/csv")
    assert repos.csv_store.get(path) == b"timestamp,rx,sub_0\n"
    repos.csv_store.delete([path])
    assert not repos.csv_store.exists(path)
    with pytest.raises(ObjectNotFound):
        repos.csv_store.get(path)
    repos.csv_store.delete([path])


def test_ping(repos):
    repos, _ = repos
    assert repos.ping() == {"database": "ok", "storage": "ok"}


def test_filter_params():
    assert to_params({"a": 1, "b": None, "c__gte": "2026", "d__in": ["x", 'y"z'], "e": True}) == [
        ("a", "eq.1"), ("b", "is.null"), ("c", "gte.2026"), ("d", 'in.("x","y\\"z")'), ("e", "eq.true")]


def test_local_store_rejects_path_escape(tmp_path):
    repos = memory_repositories(tmp_path)
    with pytest.raises(ValueError):
        repos.csv_store.put("../escape.csv", b"x", "text/csv")


def test_network_failure_becomes_unavailable():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("down", request=request)

    repos = supabase_repositories("https://example.supabase.co", "k", "a", "b", transport=httpx.MockTransport(handler))
    repos.csv_files.http.backoff = 0
    with pytest.raises(BackendUnavailable) as exc:
        repos.csv_files.list()
    assert exc.value.service == "database"
    assert len(calls) == 3
    assert repos.ping() == {"database": exc.value.message, "storage": "storage 연결 실패 (ConnectError: down)"}


def test_retry_then_success():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=[{"id": "1"}])

    repos = supabase_repositories("https://example.supabase.co", "k", "a", "b", transport=httpx.MockTransport(handler))
    repos.csv_files.http.backoff = 0
    assert repos.csv_files.list() == [{"id": "1"}]
    assert state["n"] == 3


def test_client_error_is_not_retried():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        return httpx.Response(409, json={"message": "conflict"})

    repos = supabase_repositories("https://example.supabase.co", "k", "a", "b", transport=httpx.MockTransport(handler))
    with pytest.raises(RepositoryError) as exc:
        repos.csv_files.insert({"filename": "x"})
    assert exc.value.status == 409
    assert state["n"] == 1
