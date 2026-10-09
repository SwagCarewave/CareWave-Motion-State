from __future__ import annotations

import pytest

from app.services.csv_reader import CsvValidationError, load_csi_csv
from motion_state.csi_io import SUB_COLS


def test_preview(sample_csv):
    table = load_csi_csv(sample_csv)
    p = table.preview
    assert p.receivers == ["RX1", "RX2", "RX3"]
    assert p.packets == sum(p.packets_by_rx.values())
    assert p.end_ts > p.start_ts
    assert table.offset[0] == 0.0
    assert table.amplitude.shape == (p.packets, 52)


def test_missing_columns(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("timestamp,value\n2026-10-06T22:00:00+09:00,1\n", encoding="utf-8")
    with pytest.raises(CsvValidationError) as exc:
        load_csi_csv(path)
    assert exc.value.code == "missing_columns"
    assert "rx" in exc.value.detail["missing"]


def test_no_valid_rows(tmp_path):
    path = tmp_path / "empty_rows.csv"
    header = ",".join(["experiment_id", "timestamp", "label", "rx", *SUB_COLS])
    row = ",".join(["x", "not-a-time", "", "RX9", *["1"] * 52])
    path.write_text(f"{header}\n{row}\n", encoding="utf-8")
    with pytest.raises(CsvValidationError) as exc:
        load_csi_csv(path)
    assert exc.value.code == "no_valid_rows"


def test_binary_garbage(tmp_path):
    path = tmp_path / "garbage.csv"
    path.write_bytes(b"\xff\xfe\x00\x01\x02")
    with pytest.raises(CsvValidationError):
        load_csi_csv(path)
