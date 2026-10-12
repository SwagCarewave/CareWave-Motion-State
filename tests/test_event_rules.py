from pathlib import Path

from motion_state.detector_v2 import Decision
from motion_state.monitor_v2 import NightMonitorV2

MODEL = Path(__file__).resolve().parents[1] / "models" / "lying_walking_v2.pkl"
OK = {"ok": True, "packet_rate": {}, "reason": None}


def _run(plan, until):
    """plan: (start, end, score) spans; returns the monitor and every output."""
    mon = NightMonitorV2(MODEL, baseline=0.0)
    outs, t = [], 0.0
    while t < until:
        p = next(v for a, b, v in plan if a <= t < b)
        outs.append(mon.on_decision(Decision(t, "dynamic" if p > 0.5 else "lying", p, 0.0), OK))
        t += 0.5
    return mon, outs


def test_each_walk_is_its_own_event_with_its_own_alert():
    plan = [(0, 30, 0.02), (30, 36, 0.95), (36, 56, 0.02), (56, 61, 0.9), (61, 61.5, 0.2), (61.5, 63, 0.9),
            (63, 70, 0.02)]
    mon, outs = _run(plan, 70)
    assert [o.t for o in outs if o.alert] == [32.5, 58.5]
    ended = [o.event_ended for o in outs if o.event_ended]
    assert [(e["id"], e["ended_at"], e["duration"]) for e in ended] == [(1, 35.5, 6.0), (2, 62.5, 7.0)]
    # a 0.5 s dip inside the second walk does not split it; after a walk the screen waits for the guardian
    assert outs[-1].state == "awaiting_confirmation" and outs[-1].event is None


def test_guardian_confirmation_is_per_event():
    plan = [(0, 30, 0.02), (30, 36, 0.95), (36, 56, 0.02), (56, 61, 0.9), (61, 70, 0.02)]
    mon, _ = _run(plan, 70)
    mon.guardian_close(1, "정상 활동")
    assert [e["id"] for e in mon.open_events()] == [2]


def test_short_movement_makes_no_event_and_session_end_closes_open_event():
    plan = [(0, 30, 0.02), (30, 31.5, 0.9), (31.5, 40, 0.02), (40, 45, 0.95)]
    mon, outs = _run(plan, 45)
    assert [o.t for o in outs if o.alert] == [42.5]
    assert mon.events[0]["ended_at"] is None
    summary = mon.end_session()
    assert summary["events"][0]["ended_at"] == 44.5 and summary["events"][0]["duration"] == 5.0
