from __future__ import annotations


def _save(client, name: str, minutes: int, seconds: int) -> dict:
    res = client.post("/api/maze/records", json={"name": name, "minutes": minutes, "seconds": seconds})
    assert res.status_code == 201
    return res.json()


def test_create_record(client):
    body = _save(client, "  홍길동  ", 1, 23)
    assert body["name"] == "홍길동"
    assert (body["minutes"], body["seconds"], body["duration_sec"]) == (1, 23, 83)
    assert body["id"] and isinstance(body["created_at"], float)


def test_create_rejects_invalid(client):
    for payload in ({"name": "   ", "minutes": 0, "seconds": 5},
                    {"name": "가" * 21, "minutes": 0, "seconds": 5},
                    {"name": "a", "minutes": 0, "seconds": 60},
                    {"name": "a", "minutes": -1, "seconds": 0},
                    {"name": "a", "minutes": 0}):
        res = client.post("/api/maze/records", json=payload)
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "invalid_request"


def test_ranking_order_and_ties(client):
    _save(client, "느림", 2, 0)
    _save(client, "먼저", 1, 5)
    _save(client, "빠름", 0, 59)
    _save(client, "나중", 1, 5)
    _save(client, "먼저", 3, 0)
    body = client.get("/api/maze/records").json()
    assert [(r["rank"], r["name"], r["duration_sec"]) for r in body["records"]] == [
        (1, "빠름", 59), (2, "먼저", 65), (3, "나중", 65), (4, "느림", 120), (5, "먼저", 180)]
    assert body["has_more"] is False


def test_ranking_limit_and_has_more(client):
    for i in range(12):
        _save(client, f"p{i}", 0, 30 + i)
    top = client.get("/api/maze/records", params={"limit": 10}).json()
    assert [r["name"] for r in top["records"]] == [f"p{i}" for i in range(10)]
    assert [r["rank"] for r in top["records"]] == list(range(1, 11))
    assert top["has_more"] is True
    assert client.get("/api/maze/records", params={"limit": 12}).json()["has_more"] is False
    assert len(client.get("/api/maze/records").json()["records"]) == 12
    assert client.get("/api/maze/records", params={"limit": 0}).status_code == 422


def test_ranking_empty(client):
    assert client.get("/api/maze/records", params={"limit": 10}).json() == {"records": [], "has_more": False}
