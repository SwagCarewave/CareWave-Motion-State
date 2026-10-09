from __future__ import annotations

import pytest

from app.config import FINAL_MODEL_SHA256, Settings
from app.services.csv_reader import load_csi_csv
from app.services.engine import build_engine, run_table
from app.services.model_guard import ModelIntegrityError, file_sha256, verify_model
from motion_state.monitor_v2 import NightMonitorV2
from motion_state.replay import iter_packets


def _reference(path):
    mon = NightMonitorV2(Settings().model_path)
    outs, last = [], 0.0
    for t, rx, a in iter_packets(path):
        mon.push_packet(t, rx, a)
        outs.extend(mon.poll(t))
        last = t
    outs.extend(mon.poll(last + 2.0))
    return outs, mon


def test_final_model_untouched():
    assert file_sha256(Settings().model_path) == FINAL_MODEL_SHA256


def test_verify_model_reports_bundle():
    info = verify_model(Settings().model_path, FINAL_MODEL_SHA256)
    assert info.model_type == "LogisticRegression"
    assert info.feature_count == 60
    assert info.threshold == pytest.approx(0.5, abs=1e-3)


def test_verify_model_rejects_wrong_hash():
    with pytest.raises(ModelIntegrityError):
        verify_model(Settings().model_path, "0" * 64)


def test_engine_matches_original_monitor(sample_csv):
    ref, ref_mon = _reference(sample_csv)
    table = load_csi_csv(sample_csv)
    engine = build_engine(Settings(), origin_ts=table.origin_ts)
    frames = run_table(engine, table)

    assert len(frames) == len(ref)
    for f, o in zip(frames, ref):
        assert (f.state, f.activity_score, f.motion_index, f.alert, f.candidate_sec) == \
               (o.state, o.activity_score, o.motion_index, o.alert, o.candidate_sec)
    assert [e["id"] for e in engine.events()] == [e["id"] for e in ref_mon.events]
    assert engine.summary() == ref_mon.summary()


def test_engine_frames_are_time_aligned(sample_csv):
    table = load_csi_csv(sample_csv)
    engine = build_engine(Settings(), origin_ts=table.origin_ts)
    frames = run_table(engine, table)
    steps = {round(b.ts - a.ts, 3) for a, b in zip(frames, frames[1:])}
    assert steps == {0.5}
    assert table.preview.start_ts - 0.001 <= frames[0].ts <= table.preview.end_ts
    rows = [f.heatmap for f in frames if f.heatmap is not None]
    assert len(rows) >= len(frames) - 2
    assert all(len(r) == 52 for r in rows)


def test_engine_events_use_absolute_time(sample_csv):
    table = load_csi_csv(sample_csv)
    engine = build_engine(Settings(), origin_ts=table.origin_ts)
    run_table(engine, table)
    events = engine.events()
    assert events
    for e in events:
        assert table.preview.start_ts <= e["start_ts"] <= e["alert_ts"] <= table.preview.end_ts
    closed = engine.close_event(events[0]["id"], "정상 활동")
    assert closed["guardian_result"] == "정상 활동"
    with pytest.raises(ValueError):
        engine.close_event(events[0]["id"], "없는 결과")
    with pytest.raises(KeyError):
        engine.close_event(999, "정상 활동")


def test_rx_status_transitions():
    import numpy as np

    engine = build_engine(Settings(), origin_ts=1000.0)
    assert {s["status"] for s in engine.rx_status()} == {"none"}
    amp = np.full(52, 10.0)
    for i in range(40):
        engine.push(1000.0 + i * 0.1, "RX1", amp)
        engine.push(1000.0 + i * 0.1, "RX2", amp)
        if i % 10 == 0:
            engine.push(1000.0 + i * 0.1, "RX3", amp)
    status = {s["rx"]: s for s in engine.rx_status(1004.0)}
    assert status["RX1"]["status"] == "ok"
    assert status["RX3"]["status"] == "weak"
    later = {s["rx"]: s["status"] for s in engine.rx_status(1012.0)}
    assert later == {"RX1": "lost", "RX2": "lost", "RX3": "lost"}
