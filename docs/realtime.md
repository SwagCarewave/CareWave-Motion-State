# CareWave real-time night monitor (2026-10-02)

> **Superseded.** This page describes the first real-time monitor (`motion_state/realtime.py`). Its threshold file `models/realtime_config.json` has been removed; `run_monitor.py` now uses `motion_state/monitor_v2.py` with `models/lying_walking_v2.pkl` (see `docs/service_v2.md`).

Follows the service plan "몽유병 위험군을 위한 야간 활동 감지·보호자 알림".
- Code: `motion_state/realtime.py`, run with `run_monitor.py`.
- Thresholds: `models/realtime_config.json`, fitted by `fit_realtime.py`.

## Pipeline (plan 6.2)
packets → per-RX packet-stream split (causal) → 10 Hz frames → two motion indices → baseline normalisation → internal state → persistence → event and alert.

- Only past data is used.
- One output every 0.25 s.
- Processing runs 41× faster than real time on a laptop CPU.

**Motion indices** (log of mean CSI-shape change, relative to the lying baseline):

| index | lag | window | aggregation | used for |
|---|---|---|---|---|
| `motion_index` | 0.5 s | 1 s | mean over streams | graph, tossing |
| `walk_index` | 0.3 s | 2 s | median over streams | walking / alerts |

**Baseline (plan 6.2):**
- Default: the median of the first 20 s of the session (lying still).
- Or load one from an install-time calibration recording: `--save-baseline` / `--baseline`.
- It updates slowly only while the internal state is "still".

## Output (one JSON object per tick)

| field | meaning |
|---|---|
| `t` | seconds since monitoring started |
| `state` / `state_ko` | service state (plan 5.2 / 6.3), see below |
| `internal` / `internal_ko` | `still` 가만히 누움 · `tossing` 뒤척임 · `walking` 걷기 · `unknown` 판단 보류 |
| `motion_index`, `walk_index` | indices relative to the baseline (guardian graph values) |
| `thresholds` | `tossing`, `walk_start`, `walk_end` (the end threshold is lower: hysteresis) |
| `candidate_sec` | how long the current walking-level candidate has lasted |
| `active_ratio` | share of the last 8 s at walking level |
| `event` | `{id, start, alert_t, end, duration}` of the current activity event, or null |
| `alert` | alert text, set only on the tick the alert fires, e.g. "취침 모드 중 움직임이 약 3초간 이어지고 있습니다. 현장을 확인해 주세요" |
| `signal` | `{ok, packet_rate per RX, reason}` |

**Service states:**

| state | Korean | meaning |
|---|---|---|
| `baseline_prep` | 기준선 준비 | stream split and baseline not ready yet (about 6–20 s) |
| `low_motion` | 움직임 적음 | low activity |
| `observing` | 움직임 관찰 중 | tossing, or a walking-level candidate not yet persistent |
| `sustained_activity` | 지속 활동 감지 | event created and guardian alerted |
| `signal_check` | 신호 확인 필요 | an RX is below 3 packets/s over 2 s; judgement withheld |

At the end, `night_summary` (plan 5.4) reports:
- monitored time and valid-signal time
- the number of activity events and the event list
- seconds spent in each internal state (input for the activity report)

## Results (leave-one-date-out, 107 recordings)
Persistence rule A means walking level held for 3 s (gaps up to 1 s allowed). Thresholds come from the other dates only.

| mode / rule | walks alerted | latency median / p95 | false alerts in lying+tossing (24 min) | alerts while standing |
|---|---|---|---|---|
| calibrated, A 3 s | **44/44** | 3.75 s / 5.1 s | 2 | 4 |
| calibrated, A 5 s | 35/44 | 5.75 s / 7.6 s | 1 | 1 |
| calibrated, B 8 s ≥70% | 22/44* | 8.5 s | 1 | 1 |
| uncalibrated, A 3 s | 44/44 | 3.75 s / 5.3 s | 2 | 4 |

\* Rule B needs 8 s of valid ticks. Most recordings are 12 s clips that are still warming up, so B is penalised here. Recheck it on long recordings.

**Latency:**
- It is measured from the walk start, or from the end of warm-up if later. It includes about 1 s of window lag.
- For the 5 walks that started after warm-up (the real lying → walk case), the median is 4.75 s.

**False alerts (calibrated, A 3 s):**
- hoyeon lie_down, 2:22: right after an 8 s body turn while lying. This is the "tossing ≥ 5 s" confusion case in plan 10.2.
- yena_fall_stay_down_03: lying right after a fall.
- The 4 standing alerts all come from the `sujin_stand_normal` clips. The person keeps turning her body, so they are awake and moving.

**Internal state per tick (calibrated, A 3 s; rows = truth):**

| truth → predicted | still | tossing | walking |
|---|---|---|---|
| still | 0.84 | 0.09 | 0.07 |
| tossing | 0.42 | 0.55 | 0.04 |
| walking | 0.00 | 0.02 | 0.98 |

- Walking is reliable.
- Tossing vs still is only partly separable, as in the offline analysis.
- Slow getting up and lying down also land in "tossing", because the internal state has only three classes.

**Calibrated vs uncalibrated** are nearly identical on this data.

## Limits (plan 11)
- **Too little lying time to estimate the false-alert rate:** only 24 min, mostly short clips and post-fall lying. The plan target of ≤1 false alert per 8 h cannot be checked yet. 2 alerts in 24 min would be far above it, but this data is not a real night.
- **Tossing labels:** only 2 recordings (0624, place A). The tossing threshold for the 0624 fold is in-sample.
- **Other people moving:** another person in the room produces walking-level CSI (hoyeon 1:28–1:36).
- **Plan scenarios not yet recorded:**
  - quiet getting up then stopping
  - a few steps only
  - long tossing
  - an empty room
  - a whole night
