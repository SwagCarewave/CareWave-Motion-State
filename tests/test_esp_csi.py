from __future__ import annotations

import importlib.util
import math
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.csv_reader import load_csi_csv
from app.services.esp_csi import VALID_INDEX, CsiParseError, parse_line

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("collect_csi", ROOT / "scripts" / "collect_csi.py")
collect_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect_mod)


def _legacy(line: str) -> list[float]:
    nums = list(map(int, re.search(r"\[([^\]]+)\]", line).group(1).split()))
    out = []
    for i in range(0, len(nums) - 1, 2):
        amp = math.sqrt(nums[i] ** 2 + nums[i + 1] ** 2)
        if amp > 0:
            out.append(amp)
    return out


def _line(rx="RX1", rssi=-45, seed=0, zero_at=(), extra=0) -> str:
    rng = np.random.default_rng(seed)
    pairs = rng.integers(-20, 21, size=(64, 2))
    pairs[pairs.sum(axis=1) == 0] = (1, 2)
    for i in range(64):
        if i not in VALID_INDEX:
            pairs[i] = (0, 0)
    for i in zero_at:
        pairs[i] = (0, 0)
    nums = list(pairs.reshape(-1)) + [5] * extra
    return f"CSI_DATA,{rx},24:0a:c4:00:00:01,{rssi},11,1,0,[{' '.join(str(int(n)) for n in nums)}]"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_normal_packet_matches_legacy_order_and_values():
    line = _line(seed=1)
    packet = parse_line(line)
    assert packet.rx == "RX1"
    assert packet.rssi == -45
    assert packet.amplitude.shape == (52,)
    assert np.allclose(packet.amplitude, _legacy(line))


def test_zero_subcarrier_keeps_its_column():
    line = _line(seed=2, zero_at=(5,))
    legacy = _legacy(line)
    packet = parse_line(line)
    assert len(legacy) == 51
    assert packet.amplitude.shape == (52,)
    assert packet.amplitude[4] == 0.0
    assert np.allclose(np.delete(packet.amplitude, 4), legacy)


def test_small_amplitudes_are_not_cut():
    line = "CSI_DATA,RX2,mac,-60,11,[" + " ".join(["0 0"] + ["1 0"] * 63) + "]"
    packet = parse_line(line)
    assert np.all(packet.amplitude == 1.0)


def test_longer_arrays_use_first_64_subcarriers():
    base = parse_line(_line(seed=3))
    longer = parse_line(_line(seed=3, extra=256))
    assert np.array_equal(base.amplitude, longer.amplitude)


@pytest.mark.parametrize("line,reason", [
    ("hello world", "no_array"),
    ("CSI_DATA,RX1,mac,-40,[1 2 x 4]", "bad_number"),
    ("CSI_DATA,RX1,mac,-40,[1 2 3 4]", "short_array"),
    (None, "unknown_rx"),
])
def test_rejects_bad_lines(line, reason):
    line = line or _line(rx="RX9")
    with pytest.raises(CsiParseError) as exc:
        parse_line(line)
    assert exc.value.reason == reason


def test_rx_is_normalized_and_rssi_optional():
    line = _line(rx=" rx3 ").replace(",-45,", ",n/a,")
    packet = parse_line(line)
    assert packet.rx == "RX3"
    assert packet.rssi is None


def test_collect_writes_training_format(tmp_path):
    lines = [_line(rx=rx, seed=i) for i, rx in enumerate(["RX1", "RX2", "RX3"] * 30)]
    lines += ["garbage", _line(rx="RX7")]
    out = tmp_path / "collected.csv"
    stats = collect_mod.collect(iter(lines), out, "walk", "20261010_220000")
    assert stats["saved"] == 90
    assert stats["rejected_no_array"] == 1 and stats["rejected_unknown_rx"] == 1
    header = out.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert header[:4] == ["experiment_id", "timestamp", "label", "rx"]
    assert header[4:] == [f"sub_{i}" for i in range(52)]
    table = load_csi_csv(out)
    assert table.preview.packets == 90
    assert table.preview.incomplete_rows == 0
    assert table.preview.receivers == ["RX1", "RX2", "RX3"]
    assert np.allclose(table.amplitude[0], np.round(parse_line(lines[0]).amplitude, 6))


def test_collect_cli_from_stdin(tmp_path):
    out = tmp_path / "cli.csv"
    data = "\n".join(_line(rx="RX2", seed=i) for i in range(10)) + "\n"
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "collect_csi.py"), "--stdin", "--label", "lie",
                             "--out", str(out)], input=data, capture_output=True, text=True, encoding="utf-8",
                            timeout=60)
    assert result.returncode == 0, result.stderr
    assert "저장 10" in result.stdout
    assert load_csi_csv(out).preview.packets == 10


def test_server_receives_udp_into_running_session(tmp_path):
    port = _free_port()
    settings = Settings(storage_backend="memory", local_storage_dir=tmp_path / "s", tick_sec=3600,
                        replay_tick_sec=3600, udp_enabled=True, udp_host="127.0.0.1", udp_port=port,
                        udp_flush_sec=0.05)
    with TestClient(create_app(settings)) as c:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(_line(seed=99).encode(), ("127.0.0.1", port))
            _until(lambda: c.get("/api/health").json()["collector"]["dropped_no_session"] == 1)

            sid = c.post("/api/sessions").json()["id"]
            for i in range(90):
                sender.sendto(_line(rx=("RX1", "RX2", "RX3")[i % 3], seed=i).encode(), ("127.0.0.1", port))
            sender.sendto(b"not a csi line", ("127.0.0.1", port))
            _until(lambda: c.app.state.udp.stats["accepted"] == 90 and c.app.state.udp.stats["received"] == 92)
            assert len(c.app.state.sessions.live[sid].last_rx_ts) == 3
            health = c.get("/api/health").json()["collector"]
            assert health["listening"] is True and health["port"] == port
            assert health["received"] == 92
            assert health["rejected"] == 1
            rx = {r["rx"]: r["status"] for r in c.get(f"/api/sessions/{sid}").json()["rx"]}
            assert set(rx) == {"RX1", "RX2", "RX3"}
            assert c.app.state.udp.stats["accepted"] == 90
        finally:
            sender.close()


def test_server_starts_even_if_udp_port_is_taken(tmp_path):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", 0))
    port = blocker.getsockname()[1]
    try:
        settings = Settings(storage_backend="memory", local_storage_dir=tmp_path / "s", tick_sec=3600,
                            replay_tick_sec=3600, udp_enabled=True, udp_host="127.0.0.1", udp_port=port)
        with TestClient(create_app(settings)) as c:
            health = c.get("/api/health").json()
            assert health["status"] == "ok"
            assert health["collector"]["listening"] is False
    finally:
        blocker.close()


def _until(check, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def _collector(tmp_path, **overrides):
    from app.repositories import memory_repositories
    from app.services.monitoring import SessionManager
    from app.services.udp_ingest import UdpCollector

    settings = Settings(storage_backend="memory", local_storage_dir=tmp_path / "s", tick_sec=3600, **overrides)
    manager = SessionManager(settings, memory_repositories(settings.local_storage_dir), "sha")
    return UdpCollector(settings, manager), manager


def test_udp_batch_follows_a_restarted_session(tmp_path):
    collector, manager = _collector(tmp_path)
    old, _ = manager.start()
    collector.datagram_received(_line(seed=1).encode(), ("10.0.0.5", 4000))
    collector.flush()
    manager.stop(old["id"])
    new, _ = manager.start()
    collector.datagram_received(_line(seed=2).encode(), ("10.0.0.5", 4000))
    collector.flush()
    assert collector.stats["dropped_no_session"] == 0
    assert collector.stats["accepted"] == 2
    assert "RX1" in manager.live[new["id"]].last_rx_ts


def test_udp_allowlist_rejects_unknown_sources(tmp_path):
    collector, manager = _collector(tmp_path, udp_allowed_sources=("192.168.0.21",))
    manager.start()
    collector.datagram_received(_line(seed=1).encode(), ("192.168.0.99", 4000))
    collector.datagram_received(_line(seed=2).encode(), ("192.168.0.21", 4000))
    collector.flush()
    assert collector.stats["rejected_source"] == 1
    assert collector.stats["accepted"] == 1
    assert collector.status()["allowed_sources"] == ["192.168.0.21"]
