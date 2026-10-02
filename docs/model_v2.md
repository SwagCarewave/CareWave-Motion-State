# Lying vs dynamic model v2 (2026-10-02)

## Task
- **Static:** lying, tossing/in-bed movement, standing still.
- **Dynamic:** every other labelled action, including getting up.
- One decision every 0.5 s. Each decision looks 1 s past the moment it describes, so latency is about 1 s.

## Evaluation protocol (fixed before looking at outer results)
- **Outer split:**
  - leave-one-date-out: 0929 is the only new-place fold.
  - leave-one-person-out: hoyeon / sujin / yena.
- **Inner split:** leave-one-group-out inside the outer-training groups only. Feature set, model (LR or GBM), smoothing and threshold are chosen there. Each outer test group is predicted once.
- **Scored windows:** labelled static/dynamic, more than 0.5 s from a label change. The first seconds of every recording are included.
- **Confidence intervals:** 95% bootstrap over recordings.
- **Live-equivalent input:** training frames use forward-fill (`load_csi(fill="ffill")`), exactly what the live stream can do.
  - The streaming detector (`motion_state/detector_v2.py`) reproduces the offline decisions 100% on 3 checked recordings.
  - An earlier version trained on linearly interpolated frames agreed only 75–93% with live input, and was discarded.

## Results (live-equivalent frames, 1 s look-ahead)

| protocol | accuracy (95% CI) | balanced acc | dynamic recall | static correct | AUC |
|---|---|---|---|---|---|
| leave-one-date-out | **89.2% (85.6–92.8)** | 88.9% | 91.9% | 85.9% | 0.958 |
| leave-one-person-out | **91.9% (88.9–94.5)** | 91.7% | 93.8% | 89.7% | 0.972 |

**By fold (leave-one-date-out):**

| test date | accuracy | dynamic recall | static correct |
|---|---|---|---|
| 0624 | 90.4% | 99.3% | 74.1% |
| 0625 | 86.6% | 78.8% | 95.2% |
| 0626 | 82.1% | 97.5% | 61.5% |
| 0929 (new place, June-only training) | **97.0%** | 94.1% | 99.1% |

**Per label, correct rate (date / person):**

| label | class | date | person |
|---|---|---|---|
| walking | dynamic | 98.9% | 98.5% |
| falling | dynamic | 97.4% | 98.9% |
| turning body | dynamic | 98.0% | 90.9% |
| lying | static | 91.5% | 94.4% |
| standing | static | 81.1% | 84.6% |
| getting up | dynamic | 72.4% | 86.4% |
| tossing (in-bed) | static | 37.6% | 53.9% |

**Segments:**
- Dynamic segments of 1 s or more with at least one dynamic decision: 356/357 (date), 342/344 (person).
- Static segments of 2 s or more with a false dynamic run of at least 1.5 s: 122/276 (date). Most are the first seconds of lying right after a fall.

## Same protocol, simple baseline (one feature, one threshold)

| | date acc | person acc | tossing | getting up |
|---|---|---|---|---|
| `d3_w20_med` threshold | 87.7% | 88.5% | 0.55 / 0.72 | 0.61 / 0.60 |
| `d5_w10_mean` threshold | 88.0% | 90.3% | 0.41 / 0.55 | 0.67 / 0.76 |
| **v2 GBM** | **89.2%** | **91.9%** | 0.38 / 0.54 | **0.72 / 0.86** |

**Honest reading:**
- v2 is about 1–1.5 points better overall, which is within the confidence intervals.
- It improves getting up (to 72–86%) but does not fix tossing.
- With only motion features, tossing and getting up trade off against each other.

## Why tossing stays weak
- Tossing labels exist only in two 0624 recordings (82 s).
- In the leave-one-date-out run, the 0624 fold has never seen tossing (38%).
- When the other person's tossing is in training (leave-one-person-out), it rises to 54%. More tossing recordings are the most direct fix.

## Look-ahead choice

| look-ahead | accuracy (interpolated frames, date) |
|---|---|
| 0 s | 85.6% |
| 1 s | 90.1% |
| 2 s | 89.5% |
| 3 s | 88.4% |

1 s was chosen for the shorter delay; 1 s and 2 s are equivalent within the CI. This choice was made after seeing outer results, so it is a latency decision, not a tuned gain.

## What has not been tested
- The user's private holdout. Run it with `evaluate_v2.py`; it uses the same streaming detector.
- Whole nights, an empty room, and other people or pets moving.
- A third place.

Bias note: Claude explored this same data before designing v2, so candidate features were informed by it. The private holdout and new recordings are the only fully unbiased test.

## Files
- `motion_state/features_v2.py` — features
- `build_v2.py` — feature table
- `train_v2.py` — nested CV and final model
- `motion_state/detector_v2.py` — streaming detector
- `evaluate_v2.py` — holdout evaluation through the streaming detector
- `models/lying_dynamic_v2.pkl` — GBM on 60 absolute features, threshold 0.459, 1 s look-ahead
