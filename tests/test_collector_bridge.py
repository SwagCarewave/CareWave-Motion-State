from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("collector_bridge", ROOT / "scripts" / "collector_bridge.py")
bridge_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_mod)


def _bridge(handler, session="s1"):
    b = bridge_mod.Bridge("http://test", session, batch=3, interval=0.0, buffer_max=5)
    b.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    return b


def _packet(i):
    return {"ts": 1000.0 + i, "rx": "RX1", "amplitude": [1.0] * 52}


def test_read_rows_parses_collector_csv(sample_csv):
    with sample_csv.open(encoding="utf-8-sig", newline="") as f:
        rows = []
        for row in bridge_mod.read_rows(f):
            rows.append(row)
            if len(rows) == 5:
                break
    assert rows[0]["rx"] in ("RX1", "RX2", "RX3")
    assert len(rows[0]["amplitude"]) == 52
    assert rows[1]["ts"] >= rows[0]["ts"]


def test_read_rows_marks_missing_values():
    header = "timestamp,rx," + ",".join(f"sub_{i}" for i in range(52))
    line = "2026-10-06T22:00:00+09:00,rx1," + ",".join(["1"] * 51) + ","
    rows = list(bridge_mod.read_rows(io.StringIO(f"{header}\n{line}\n")))
    assert rows[0]["rx"] == "RX1"
    assert rows[0]["amplitude"][-1] is None


def test_keeps_buffer_while_server_down_then_resends():
    state = {"down": True, "sent": []}

    def handler(request):
        if state["down"]:
            raise httpx.ConnectError("down", request=request)
        packets = json.loads(request.read())["packets"]
        state["sent"].extend(p["ts"] for p in packets)
        return httpx.Response(200, json={"accepted": len(packets), "rejected_invalid": 0, "rejected_incomplete": 0,
                                         "rejected_stale": 0, "frames": 0})

    b = _bridge(handler)
    for i in range(4):
        b.add(_packet(i))
    assert len(b.buffer) == 4
    state["down"] = False
    b.drain(timeout=5)
    assert state["sent"] == [1000.0, 1001.0, 1002.0, 1003.0]
    assert not b.buffer
    assert b.stats["accepted"] == 4


def test_buffer_is_bounded():
    def handler(request):
        return httpx.Response(503)

    b = _bridge(handler)
    for i in range(8):
        b.add(_packet(i))
    assert len(b.buffer) == 5
    assert b.dropped == 3
    assert b.buffer[0]["ts"] == 1003.0


def test_stopped_session_starts_new_one():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/sessions":
            return httpx.Response(201, json={"id": "s2", "created": True})
        if "/s1/" in request.url.path:
            return httpx.Response(409, json={"error": {"code": "session_stopped"}})
        return httpx.Response(200, json={"accepted": 1, "rejected_invalid": 0, "rejected_incomplete": 0,
                                         "rejected_stale": 0, "frames": 0})

    b = _bridge(handler)
    b.buffer.append(_packet(0))
    b.drain(timeout=5)
    assert calls == ["/api/sessions/s1/packets", "/api/sessions", "/api/sessions/s2/packets"]
    assert b.session_id == "s2"
    assert not b.buffer


def test_read_rows_survives_broken_values():
    header = "timestamp,rx," + ",".join(f"sub_{i}" for i in range(52))
    broken = "2026-10-06T22:00:00+09:00,RX1," + ",".join(["abc", "nan", "inf"] + ["1"] * 49)
    good = "2026-10-06T22:00:01+09:00,RX2," + ",".join(["2"] * 52)
    rows = list(bridge_mod.read_rows(io.StringIO(f"{header}\n{broken}\n{good}\n")))
    assert rows[0]["amplitude"][:3] == [None, None, None]
    assert rows[1]["amplitude"] == [2.0] * 52
    json.dumps(rows, allow_nan=False)
