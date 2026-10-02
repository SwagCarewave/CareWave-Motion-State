# CareWave night monitor v2 (2026-10-02)

## What changed
- **Decision step:** the walking-level decision now uses the model `models/lying_walking_v2.pkl`.
  - Classes: lying (still + tossing) vs walking.
  - Logistic regression on 60 motion features, threshold 0.50.
  - Nested CV balanced accuracy 95.7%.
- **Service layer** (`motion_state/monitor_v2.py`) keeps the plan's outputs:
  - states: 기준선 준비 / 움직임 적음 / 움직임 관찰 중 / 지속 활동 감지 / 신호 확인 필요
  - motion index and thresholds
  - current candidate duration
  - event ID, start, alert time and duration
  - alert text "취침 모드 중 움직임이 약 N초간 이어지고 있습니다. 현장을 확인해 주세요"
  - night summary with activity event count
- The internal 눕기/걷기 label is a research log only (plan 10.1).

**Technical deviations from the plan (allowed by the user):**
- Decisions every 0.5 s instead of 0.25 s.
- Each decision looks 1 s past the moment it describes, so each output arrives about 1 s late. That second is part of the reported latency.

## Service-level evaluation (plan 11), leave-one-date-out
- Fold models use the configuration chosen by nested CV on the other dates.
- Every recording is replayed packet by packet through the streaming detector.

| persistence rule | walks after warm-up alerted | latency median / p95 (walk start → alert output) | walks started in warm-up alerted | false alerts, lying + tossing (16.8 min) | tossing segments alerted |
|---|---|---|---|---|---|
| A 2 s | 12/12 | 3.75 / 4.5 s | 15/15 | 2 | 2/25 |
| **A 3 s (default)** | **11/11** | **4.75 / 5.5 s** | 10/10 | **1** | 1/25 |
| A 5 s | 10/10 | 6.75 / 7.5 s | 10/10 | 0 | 0/25 |
| B 8 s ≥ 70% | 10/10 | 7.75 / 8.5 s | 9/10 | 1 | 0/25 |

- **Walks not scored:** 20–27, mostly 12 s clips. The detector warm-up (about 10 s) leaves less walking than the hold time.
- **Only false alert (A 3 s):** hoyeon_lie_down_normal_01 at 1:10, a 4 s body turn while lying.
- **Alerts during other real activity** (getting up, falls, arm movement): 23 (A 3 s). These count as technically correct activity.
- **Selection bias:** the default rule (A 3 s) was picked after seeing these results.

## Limits and demo risks
See the conversation summary. Main points:
- **Warm-up:**
  - about 10 s before the first decision
  - about 20 s of quiet data before the motion-index baseline
- **8-hour false-alert rate:** cannot be estimated from 17 minutes of lying.
- **Other people moving** cause walking-level CSI.
- **Getting up then standing still** gives no alert.
- **Long tossing** (4 s or more) can alert.
- **Device and room changes** are untested beyond 2 places and one layout.
- **Collector output** must match the raw CSV columns for `--stdin`.

## Update: movement-level labels and guardian confirmation (plan 6.3)
- **Internal label renamed:** "걷기 수준" / "걷기 수준 아님" (was 걷기/눕기). The model does not know posture.
  - After a walking alert, non-walking activity reads as "걷기 수준 아님" about half the time: standing 52%, arm movement 49%, getting up 51%.
- **Alerted events stay open** until `guardian_close(id, result)` is called, with result one of 확인 중 / 정상 활동 / 도움 필요 / 잘못된 감지.
  - When activity drops, only `detection_end` is set.
  - While any alerted event is unconfirmed, the screen shows "보호자 확인 대기" instead of "움직임 적음".
  - Walking-level activity more than 30 s after detection end creates a new event and a new alert.
- **Service evaluation is unchanged:** rule A 3 s caught 11/11 walks after warm-up, with 1 false alert.
