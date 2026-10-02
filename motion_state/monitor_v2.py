"""Night monitor v2: service plan outputs on top of the lying/walking model.

Raw CSI packets in; every decision step (0.5 s) one MonitorOutput out.
  * model: LyingDynamicDetector (lying incl. tossing vs walking), decision 1 s after the moment
  * service states (plan 5.2 / 6.3): 기준선 준비 / 움직임 적음 / 움직임 관찰 중 /
    지속 활동 감지 / 신호 확인 필요
  * persistence (plan 6.1 / 6.4): rule A (continuous with short gaps) or rule B (active share)
  * events and alerts (plan 5.3), night summary (plan 5.4)
The model measures movement level, not posture: the internal label is "걷기 수준" vs
"걷기 수준 아님" (research log only, plan 10.1). A person who stops walking and waves their
arms is "걷기 수준 아님", not "lying". After an alert the event stays open until the guardian
closes it (plan 6.3): a drop in activity only ends detection, it never declares a safe return.
"""
from __future__ import annotations

import itertools
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .csi_io import RX_IDS
from .detector_v2 import Decision, LyingDynamicDetector

STATE_KO = {
    "baseline_prep": "기준선 준비",
    "low_motion": "움직임 적음",
    "observing": "움직임 관찰 중",
    "sustained_activity": "지속 활동 감지",
    "signal_check": "신호 확인 필요",
    "awaiting_confirmation": "보호자 확인 대기",
}
INTERNAL_KO = {"below_walking": "걷기 수준 아님", "walking_level": "걷기 수준", "unknown": "판단 보류"}
GUARDIAN_RESULTS = ("확인 중", "정상 활동", "도움 필요", "잘못된 감지")


@dataclass
class ServiceConfig:
    persistence: str = "continuous"     # "continuous" (rule A) | "ratio" (rule B)
    hold_sec: float = 3.0               # rule A: active this long (gaps <= gap_sec allowed)
    gap_sec: float = 1.0
    ratio_window_sec: float = 8.0       # rule B
    ratio_min: float = 0.7
    end_ratio: float = 0.75             # end threshold = end_ratio * model threshold (hysteresis)
    event_merge_sec: float = 30.0       # activity resuming within this time continues the event
    calibration_sec: float = 20.0       # motion-index baseline from the first quiet seconds
    min_packet_rate: float = 3.0        # per RX, packets/s over signal_window_sec
    signal_window_sec: float = 2.0


@dataclass
class MonitorOutput:
    t: float                    # moment described (s since start); output arrives ~1 s later
    state: str
    state_ko: str
    internal: str               # research log only
    internal_ko: str
    motion_index: float | None  # graph value: log motion relative to the quiet baseline
    activity_score: float | None  # model probability of walking-level activity
    thresholds: dict
    candidate_sec: float        # current candidate duration
    event: dict | None
    alert: str | None           # set only on the step an alert fires
    signal: dict

    def to_dict(self) -> dict:
        return asdict(self)


class NightMonitorV2:
    def __init__(self, model_path: Path = Path("models/lying_walking_v2.pkl"),
                 config: ServiceConfig | None = None, baseline: float | None = None):
        self.cfg = config or ServiceConfig()
        self.det = LyingDynamicDetector(model_path)
        self.start_thr = self.det.threshold
        self.end_thr = self.cfg.end_ratio * self.det.threshold
        self.packet_times = {rx: deque() for rx in RX_IDS}
        self.baseline = baseline
        self.calib: list[float] = []
        self.active = False
        self.run_start: float | None = None
        self.last_active: float | None = None
        self.hist: deque = deque()
        self.event: dict | None = None
        self.events: list[dict] = []
        self.ids = itertools.count(1)
        self.seconds = {"below_walking": 0.0, "walking_level": 0.0, "unknown": 0.0, "signal_check": 0.0}
        self.t0: float | None = None

    # ---------------- input ----------------
    def push_packet(self, t: float, rx: str, amplitude: np.ndarray) -> None:
        if self.t0 is None:
            self.t0 = t
        if rx in self.packet_times:
            self.packet_times[rx].append(t - self.t0)
        self.det.push_packet(t, rx, amplitude)

    def poll(self, now: float) -> list[MonitorOutput]:
        if self.t0 is None:
            return []
        signal = self._signal(now - self.t0)
        return [self.on_decision(d, signal) for d in self.det.poll(now)]

    def _signal(self, rel_now: float) -> dict:
        rates = {}
        for rx, q in self.packet_times.items():
            while q and q[0] < rel_now - self.cfg.signal_window_sec:
                q.popleft()
            rates[rx] = round(len(q) / self.cfg.signal_window_sec, 1)
        warm = rel_now >= self.cfg.signal_window_sec
        bad = [rx for rx, r in rates.items() if warm and r < self.cfg.min_packet_rate]
        return {"ok": not bad, "packet_rate": rates, "reason": f"패킷 부족: {', '.join(bad)}" if bad else None}

    # ---------------- decision -> service state ----------------
    def on_decision(self, d: Decision, signal: dict) -> MonitorOutput:
        """Public so cached decisions can be re-run through the same logic (evaluation)."""
        cfg, step = self.cfg, 0.5
        thresholds = {"activity_start": round(self.start_thr, 4), "activity_end": round(self.end_thr, 4)}

        def out(state, internal, mi=None, alert=None):
            self.seconds["signal_check" if state == "signal_check" else internal] += step
            return MonitorOutput(round(d.t, 2), state, STATE_KO[state], internal, INTERNAL_KO[internal],
                                 None if mi is None else round(mi, 4), d.probability, thresholds,
                                 round(self._candidate(), 2), None if self.event is None else dict(self.event),
                                 alert, signal)

        if not signal["ok"]:
            self._set_active(d.t, False)
            return out("signal_check", "unknown")
        if d.state == "warming_up" or d.probability is None:
            return out("baseline_prep", "unknown")
        p = d.probability
        # hysteresis: start threshold to switch on, lower end threshold to switch off
        self.active = p >= (self.end_thr if self.active else self.start_thr)
        if self.baseline is None:
            if not self.active:
                self.calib.append(d.motion)
            if len(self.calib) * step >= cfg.calibration_sec:
                self.baseline = float(np.median(self.calib))
        mi = None if self.baseline is None else d.motion - self.baseline
        internal = "walking_level" if self.active else "below_walking"
        self._set_active(d.t, self.active)

        alert = None
        cur = self.event          # event that new activity would merge into
        if cur is not None and cur["detection_end"] is not None and d.t - cur["detection_end"] > cfg.event_merge_sec:
            cur = self.event = None   # merge window over: new activity becomes a new event
        if self.active and self._persistent(d.t):
            if cur is None:
                self.event = {"id": next(self.ids), "start": round(self.run_start, 2), "alert_t": round(d.t, 2),
                              "detection_end": None, "duration": 0.0, "guardian": None}
                self.events.append(self.event)
                alert = (f"취침 모드 중 움직임이 약 {max(1, round(d.t - self.run_start))}초간 이어지고 있습니다. "
                         f"현장을 확인해 주세요")
            state = "sustained_activity"
        elif cur is not None and self.active:
            state = "sustained_activity"           # same event continues (no duplicate alert)
        elif self.active or self.run_start is not None:
            state = "observing"
        else:
            state = "low_motion"
        if self.event is not None:
            ev = self.event
            if self.active:
                ev["detection_end"] = None
            elif ev["detection_end"] is None:
                ev["detection_end"] = round(d.t, 2)      # detection ended, situation NOT closed
            ev["duration"] = round((ev["detection_end"] or d.t) - ev["start"], 2)
        # an alerted event the guardian has not closed keeps the screen out of "움직임 적음"
        if state == "low_motion" and self.open_events():
            state = "awaiting_confirmation"
        if self.baseline is None:
            state = "baseline_prep" if state == "low_motion" else state
        return out(state, internal, mi, alert)

    def open_events(self) -> list[dict]:
        return [e for e in self.events if e["guardian"] is None]

    def guardian_close(self, event_id: int, result: str) -> None:
        """Guardian's 'situation closed' input (plan 5.3). Kept separate from detection end."""
        if result not in GUARDIAN_RESULTS:
            raise ValueError(f"result must be one of {GUARDIAN_RESULTS}")
        for e in self.events:
            if e["id"] == event_id:
                e["guardian"] = result

    def _set_active(self, t: float, active: bool) -> None:
        self.hist.append((t, active))
        while self.hist and self.hist[0][0] <= t - self.cfg.ratio_window_sec:
            self.hist.popleft()
        if active:
            if self.run_start is None:
                self.run_start = t - 0.5
            self.last_active = t
        elif self.run_start is not None and t - self.last_active > self.cfg.gap_sec:
            self.run_start = None

    def _candidate(self) -> float:
        return 0.0 if self.run_start is None else (self.last_active - self.run_start)

    def _persistent(self, t: float) -> bool:
        if self.cfg.persistence == "ratio":
            full = bool(self.hist) and self.hist[0][0] <= t - self.cfg.ratio_window_sec + 1.0
            return full and np.mean([a for _, a in self.hist]) >= self.cfg.ratio_min
        return self.run_start is not None and self._candidate() >= self.cfg.hold_sec

    def summary(self) -> dict:
        """Night record (plan 5.4). Activity events, never "sleepwalking count"."""
        monitored = sum(self.seconds.values())
        return {
            "monitored_sec": round(monitored, 1),
            "valid_signal_sec": round(monitored - self.seconds["signal_check"], 1),
            "activity_events": len(self.events),
            "unconfirmed_events": len(self.open_events()),
            "events": [dict(e) for e in self.events],
            "seconds_by_internal_state(research)": {INTERNAL_KO[k]: round(v, 1) for k, v in self.seconds.items()
                                                    if k in INTERNAL_KO},
        }
