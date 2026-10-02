"""Real-time night monitor: raw CSI packets in, service state + internal state out.

Pipeline (service plan 6.2): packets -> per-RX packet-stream split -> 10 Hz frames ->
motion indices on 1 s / 2 s windows -> baseline normalisation -> internal state
(still / tossing / walking) -> persistence -> events and alerts.

Only past data is used. Every TICK_SEC the monitor emits one MonitorOutput.
"""
from __future__ import annotations

import itertools
from collections import deque
from dataclasses import asdict, dataclass, field

import numpy as np

from .csi_io import N_SUB, RX_IDS

FPS = 10
N_STREAMS = len(RX_IDS) * 2

STATE_KO = {
    "baseline_prep": "기준선 준비",
    "low_motion": "움직임 적음",
    "observing": "움직임 관찰 중",
    "sustained_activity": "지속 활동 감지",
    "signal_check": "신호 확인 필요",
}
INTERNAL_KO = {"still": "가만히 누움", "tossing": "뒤척임", "walking": "걷기", "unknown": "판단 보류"}


@dataclass
class MonitorConfig:
    tick_sec: float = 0.25
    # motion indices: log of mean spectrum distance; values are relative to the lying baseline
    toss_lag_frames: int = 5          # 0.5 s lag, 1 s window: sensitive to small in-bed motion
    toss_win_frames: int = 10
    walk_lag_frames: int = 3          # 0.3 s lag, 2 s window: robust for walking
    walk_win_frames: int = 20
    # thresholds in log units above the baseline median (fitted by fit_realtime.py)
    toss_threshold: float = 0.06
    walk_start: float = 0.42
    walk_end: float = 0.30            # lower end threshold (hysteresis)
    # persistence: "continuous" = rule A (walk_hold_sec continuous, gaps <= gap_sec allowed)
    #              "ratio"      = rule B (active share of last ratio_window_sec >= ratio_min)
    persistence: str = "continuous"
    walk_hold_sec: float = 3.0
    gap_sec: float = 1.0
    ratio_window_sec: float = 8.0
    ratio_min: float = 0.7
    event_merge_sec: float = 30.0     # activity resuming within this time continues the same event
    # baseline from a calibration period of quiet lying (median of the indices)
    calibration_sec: float = 20.0
    baseline_update: bool = True      # slow update only while internal state is still
    baseline_alpha: float = 0.002
    # signal quality
    stream_init_packets: int = 60
    min_packet_rate: float = 3.0      # per RX, packets/s over the last signal_window_sec
    signal_window_sec: float = 2.0

    def save(self, path) -> None:
        import json
        from pathlib import Path
        Path(path).write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path) -> "MonitorConfig":
        import json
        from pathlib import Path
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass
class MonitorOutput:
    t: float
    state: str
    state_ko: str
    internal: str
    internal_ko: str
    motion_index: float | None        # 1 s index relative to baseline (graph value)
    walk_index: float | None          # 2 s index relative to baseline (alert value)
    thresholds: dict
    candidate_sec: float
    active_ratio: float
    event: dict | None
    alert: str | None                 # set only on the tick an alert is raised
    signal: dict

    def to_dict(self) -> dict:
        return asdict(self)


class _StreamSplitter:
    """Causal split of one RX's packets into its two interleaved spectral streams."""

    def __init__(self, init_packets: int, ema_alpha: float = 0.05):
        self.init_packets = init_packets
        self.ema_alpha = ema_alpha
        self.buffer: list[tuple[float, np.ndarray]] = []
        self.centroids: np.ndarray | None = None

    def push(self, t: float, shape: np.ndarray) -> list[tuple[float, int, np.ndarray]]:
        if self.centroids is None:
            self.buffer.append((t, shape))
            if len(self.buffer) < self.init_packets:
                return []
            from sklearn.cluster import KMeans
            shapes = np.stack([s for _, s in self.buffer])
            c = KMeans(2, n_init=5, random_state=0).fit(shapes).cluster_centers_
            c /= np.linalg.norm(c, axis=1, keepdims=True)
            half = shapes.shape[1] // 2
            if (c[0, :half] ** 2).sum() < (c[1, :half] ** 2).sum():
                c = c[::-1].copy()
            self.centroids = c
            pending, self.buffer = self.buffer, []
            return [self._assign(tt, s) for tt, s in pending]
        return [self._assign(t, shape)]

    def _assign(self, t: float, shape: np.ndarray) -> tuple[float, int, np.ndarray]:
        k = int(np.argmax(self.centroids @ shape))
        self.centroids[k] = (1 - self.ema_alpha) * self.centroids[k] + self.ema_alpha * shape
        self.centroids[k] /= np.linalg.norm(self.centroids[k])
        return t, k, shape


class NightMonitor:
    """Feed packets with push_packet(); call poll(now) to get outputs for elapsed ticks."""

    def __init__(self, config: MonitorConfig | None = None, baseline: dict | None = None):
        self.cfg = config or MonitorConfig()
        self.splitters = {rx: _StreamSplitter(self.cfg.stream_init_packets) for rx in RX_IDS}
        self.t0: float | None = None
        self.bins: dict[int, dict[int, list[np.ndarray]]] = {}
        self.last_shape = np.full((N_STREAMS, N_SUB), np.nan)
        self.frames: deque[np.ndarray] = deque(maxlen=self.cfg.walk_win_frames + 40)
        self.next_frame = 0
        self.packet_times = {rx: deque() for rx in RX_IDS}
        self.next_tick: float | None = None
        # baseline: {"toss": median, "walk": median}
        self.baseline = dict(baseline) if baseline else None
        self.calib: list[tuple[float, float]] = []
        # persistence / events
        self.active_hist: deque[tuple[float, bool]] = deque()
        self.run_start: float | None = None
        self.last_active: float | None = None
        self.walk_on = False
        self.event: dict | None = None
        self.events: list[dict] = []
        self.event_ids = itertools.count(1)
        self.counts = {k: 0.0 for k in INTERNAL_KO} | {"signal_check": 0.0, "baseline_prep": 0.0}

    # ---------------- input ----------------
    def push_packet(self, t: float, rx: str, amplitude: np.ndarray) -> None:
        """t: seconds (any origin, increasing); amplitude: 52 subcarrier amplitudes."""
        if rx not in self.splitters or not np.all(np.isfinite(amplitude)):
            return
        norm = float(np.linalg.norm(amplitude))
        if norm <= 0:
            return
        if self.t0 is None:
            self.t0 = t
            self.next_tick = self.cfg.tick_sec
        rel = t - self.t0
        self.packet_times[rx].append(rel)
        for tt, k, shape in self.splitters[rx].push(rel, amplitude / norm):
            stream = RX_IDS.index(rx) * 2 + k
            b = int(np.rint(tt * FPS))
            self.bins.setdefault(b, {}).setdefault(stream, []).append(shape)

    # ---------------- output ----------------
    def poll(self, now: float) -> list[MonitorOutput]:
        """Emit outputs for every tick up to `now` (same clock as push_packet)."""
        if self.t0 is None:
            return []
        rel_now = now - self.t0
        self._close_frames(rel_now)
        outs = []
        while self.next_tick <= rel_now:
            outs.append(self._tick(self.next_tick))
            self.next_tick += self.cfg.tick_sec
        return outs

    def _close_frames(self, rel_now: float) -> None:
        # a 10 Hz frame is final once the clock is 0.1 s past it (allows small packet jitter)
        last_final = int(np.floor(rel_now * FPS)) - 1
        while self.next_frame <= last_final:
            got = self.bins.pop(self.next_frame, {})
            frame = self.last_shape.copy()
            for stream, shapes in got.items():
                s = np.mean(shapes, axis=0)
                frame[stream] = s / np.linalg.norm(s)
            self.last_shape = frame
            self.frames.append(frame)
            self.next_frame += 1

    def _indices(self) -> tuple[float, float] | None:
        cfg = self.cfg
        need = max(cfg.toss_win_frames + cfg.toss_lag_frames, cfg.walk_win_frames + cfg.walk_lag_frames)
        if len(self.frames) < need:
            return None
        f = np.stack(self.frames)
        if np.isnan(f[-need:]).any():
            return None

        def index(lag: int, win: int, agg) -> float:
            d = np.linalg.norm(f[-win:] - f[-win - lag:-lag], axis=2)   # (win, streams)
            return float(np.log(agg(d.mean(axis=0)) + 1e-6))

        return index(cfg.toss_lag_frames, cfg.toss_win_frames, np.mean), \
            index(cfg.walk_lag_frames, cfg.walk_win_frames, np.median)

    def _signal(self, t: float) -> dict:
        rates = {}
        for rx, q in self.packet_times.items():
            while q and q[0] < t - self.cfg.signal_window_sec:
                q.popleft()
            rates[rx] = round(len(q) / self.cfg.signal_window_sec, 1)
        warm = t >= self.cfg.signal_window_sec
        bad = [rx for rx, r in rates.items() if warm and r < self.cfg.min_packet_rate]
        return {"ok": not bad, "packet_rate": rates,
                "reason": f"패킷 부족: {', '.join(bad)}" if bad else None}

    def _tick(self, t: float) -> MonitorOutput:
        return self.decide(t, self._indices(), self._signal(t))

    def decide(self, t: float, idx: tuple[float, float] | None, signal: dict) -> MonitorOutput:
        """State logic for one tick, given raw (toss, walk) indices and signal status.
        Public so cached indices can be re-run through the exact same logic."""
        cfg = self.cfg
        thresholds = {"tossing": cfg.toss_threshold, "walk_start": cfg.walk_start, "walk_end": cfg.walk_end}

        def out(state, internal, mi=None, wi=None, alert=None):
            self.counts[internal if state not in ("signal_check", "baseline_prep") else state] += cfg.tick_sec
            return MonitorOutput(
                t=round(t, 2), state=state, state_ko=STATE_KO[state], internal=internal,
                internal_ko=INTERNAL_KO[internal], motion_index=None if mi is None else round(mi, 4),
                walk_index=None if wi is None else round(wi, 4), thresholds=thresholds,
                candidate_sec=round(self._candidate_sec(t), 2), active_ratio=round(self._ratio(t), 3),
                event=None if self.event is None else dict(self.event), alert=alert, signal=signal)

        if not signal["ok"]:
            self._set_active(t, False)
            return out("signal_check", "unknown")
        if idx is None:
            return out("baseline_prep", "unknown")
        toss_raw, walk_raw = idx
        if self.baseline is None:
            self.calib.append((toss_raw, walk_raw))
            if len(self.calib) * cfg.tick_sec < cfg.calibration_sec:
                return out("baseline_prep", "unknown")
            c = np.array(self.calib)
            self.baseline = {"toss": float(np.median(c[:, 0])), "walk": float(np.median(c[:, 1]))}
        mi = toss_raw - self.baseline["toss"]
        wi = walk_raw - self.baseline["walk"]

        # internal state with hysteresis on the walking level
        self.walk_on = wi >= (cfg.walk_end if self.walk_on else cfg.walk_start)
        internal = "walking" if self.walk_on else ("tossing" if mi >= cfg.toss_threshold else "still")
        if internal == "still" and cfg.baseline_update:
            a = cfg.baseline_alpha
            self.baseline["toss"] += a * (toss_raw - self.baseline["toss"])
            self.baseline["walk"] += a * (walk_raw - self.baseline["walk"])

        self._set_active(t, self.walk_on)
        alert = None
        if self._persistent(t):
            if self.event is None:
                self.event = {"id": next(self.event_ids), "start": round(self.run_start, 2),
                              "alert_t": round(t, 2), "end": None, "duration": 0.0}
                self.events.append(self.event)
                alert = (f"취침 모드 중 움직임이 약 {t - self.run_start:.0f}초간 이어지고 있습니다. "
                         f"현장을 확인해 주세요")
            state = "sustained_activity"
        elif self.event is not None and self.last_active is not None and t - self.last_active <= cfg.event_merge_sec \
                and self.walk_on:
            state = "sustained_activity"
        else:
            state = "observing" if internal != "still" or self.run_start is not None else "low_motion"
        if self.event is not None:
            if self.walk_on:
                self.event["end"] = None
            elif self.event["end"] is None:
                self.event["end"] = round(t, 2)
            self.event["duration"] = round((self.event["end"] or t) - self.event["start"], 2)
            if self.event["end"] is not None and t - self.event["end"] > cfg.event_merge_sec:
                self.event = None
        return out(state, internal, mi, wi, alert)

    # ---------------- persistence ----------------
    def _set_active(self, t: float, active: bool) -> None:
        cfg = self.cfg
        self.active_hist.append((t, active))
        while self.active_hist and self.active_hist[0][0] <= t - cfg.ratio_window_sec:
            self.active_hist.popleft()
        if active:
            if self.run_start is None:
                self.run_start = t - cfg.tick_sec
            self.last_active = t
        elif self.run_start is not None and t - self.last_active > cfg.gap_sec:
            self.run_start = None

    def _candidate_sec(self, t: float) -> float:
        if self.run_start is None:
            return 0.0
        return (self.last_active or t) - self.run_start

    def _ratio(self, t: float) -> float:
        if not self.active_hist:
            return 0.0
        return sum(a for _, a in self.active_hist) / len(self.active_hist)

    def _persistent(self, t: float) -> bool:
        cfg = self.cfg
        if cfg.persistence == "ratio":
            # the window must be filled with valid ticks, not just elapsed clock time
            full = bool(self.active_hist) and self.active_hist[0][0] <= t - cfg.ratio_window_sec + 2 * cfg.tick_sec
            return full and self._ratio(t) >= cfg.ratio_min and self.walk_on
        return self.run_start is not None and self.walk_on and self._candidate_sec(t) >= cfg.walk_hold_sec

    def summary(self) -> dict:
        """Night record (service plan 5.4)."""
        monitored = sum(self.counts.values())
        return {
            "monitored_sec": round(monitored, 1),
            "valid_signal_sec": round(monitored - self.counts["signal_check"], 1),
            "activity_events": len(self.events),
            "events": [dict(e) for e in self.events],
            "seconds_by_internal_state": {INTERNAL_KO[k]: round(self.counts[k], 1) for k in INTERNAL_KO},
        }
