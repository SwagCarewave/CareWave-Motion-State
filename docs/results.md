# Motion State v1: static vs dynamic from CSI raw (2026-10-02)

## Task
- **Static:** lying, standing. Tossing while lying counts as static.
- **Dynamic:** walking, falling, getting up/transition, bending/straightening, raising arms, turning the body, turning the head.
- **Input:** CSI raw only (3 RX × 2 packet streams × 52 subcarriers, 10 Hz).
- **Output:** a static/dynamic decision every 0.5 s.
  - It uses only the past 1/2/4 s, so it works in real time.
  - The label is taken at the window centre, giving about 1 s of latency.

## Data and split
- 107 recordings copied from CareWave-Action-Model. The user's private holdout (about 10 recordings) is excluded.
- Main evaluation is **leave-one-date-out**: train on three dates, test on the fourth.
  - 0624 / 0625 / 0626 are place A. Each fold tests "same place, new day".
  - 0929 is place B. That fold is "train on June, test on September (new place)".
- Splits are by whole recording. Windows from one recording are never split between train and test.
- Scoring excludes windows within 1 s of a static/dynamic change, because labels are whole seconds.

| date | dynamic windows | static windows |
|---|---|---|
| 0624 | 1269 | 1039 |
| 0625 | 1622 | 1456 |
| 0626 | 968 | 804 |
| 0929 | 926 | 1221 |

## Model (`motion_state/model.py`)
- Motion features per rx-stream:
  - packet-to-packet spectrum change
  - lag 0.3 s and 1 s spectrum distance
  - spectrum spread
  - amplitude CV
  - 0.5 s burst peak
- Features are log-transformed and aggregated over the 6 streams (median, max, min) for 1/2/4 s windows.
- **Relative features:** each feature minus its 10th percentile over the past 60 s of the same recording. This removes the room/day noise floor causally.
- Classifier: logistic regression.
- Threshold: set on the training data so that 3% of static windows are read as dynamic. It does not depend on which motions a date contains.

## Results (leave-one-date-out, final model)

| test date | BAcc | AUC | walking | falling | getting up/transition | arms/torso | lying FA | standing FA |
|---|---|---|---|---|---|---|---|---|
| 0624 (A) | 0.914 | 0.980 | 0.98 | 1.00 | 0.57 | – | 0.04 | 0.36 |
| 0625 (A) | 0.741 | 0.947 | 0.93 | 0.81 | 0.22 | 0.53 | 0.00 | 0.06 |
| 0626 (A) | 0.909 | 0.972 | 0.96 | 1.00 | 0.96 | 0.81 | 0.11 | 0.14 |
| 0929 (B, new place) | **0.910** | 0.937 | 0.92 | 0.91 | 0.82 | – | 0.08 | 0.07 |
| mean | 0.868 | 0.959 | 0.95 | 0.93 | 0.64 | 0.67 | 0.06 | 0.16 |

Values are the share of windows read as dynamic. For lying and standing, that share is the false-alarm rate (FA).

**Segment view:** a segment counts as detected if any of its windows is dynamic.
- **Walking:** 47/47 segments detected. Still 46/47 when 3 s of continuous dynamic is required.
- **Head turn:** 0/2. Only 6 s of data exist.

**Baselines (mean BAcc):**
- one-feature threshold: 0.824
- LR without relative features: 0.848
- GBM: 0.866, but it fails differently on each date (lying FA up to 35%)

## Interpretation
1. **Lying (very quiet) vs walking (very loud) is robust.** This also holds at the new place (0929). This is the core sequence for sleepwalking detection (lying → walking).
2. **Slow, small motions are the weak point.** Slow getting up, raising arms and turning the body are detected 22–96% depending on the date.
   - Within one recording, getting up is clearly louder than lying.
   - But in some recordings, **swaying while standing is just as large**. Examples: the 0624 standing recording, and the 0626 standing recordings.
   - If the threshold is lowered to catch small motions, standing false alarms rise. This trade-off is unavoidable with motion strength alone.
3. **The decision threshold shifts by date and place.** AUC is 0.94–0.98, but BAcc is lower. The relative (floor-normalised) features partly fix this.

## Can CSI tell standing from lying?

| method | result |
|---|---|
| motion strength (standing sways more) | AUC 0.94 on 0624, but 0.55–0.63 on other dates. Not reliable. |
| spectrum shape, leave-one-date-out | AUC 0.10–0.55 (inverted on 0625). **Does not generalise.** |
| spectrum shape, dates mixed | AUC 0.82. It mostly learns that place/day. |
| spectrum change within one recording | Posture change is visible: lying-vs-standing distance is 2–4× the within-posture variation in 6/10 recordings. But which side is lying is not consistent. |

**Conclusion: lying vs standing as an absolute classification across places and days is not possible with the current data.**

What is possible is noticing that the posture changed. For the service, these approaches are realistic:
- **Calibrate per place:** record a reference spectrum of the person lying in bed (for example 30 s at the start of measurement). Then compare current static periods with that "in bed" reference.
- **Context:** a static period that follows walking is standing or out of bed. A static period that follows lying down is lying.

## Limits
- **Too little long lying data:**
  - Only about 7 minutes of lying segments last 30 s or more.
  - Measured false alarms while lying: 32/h, with a 2 s minimum dynamic run.
  - Most of that lying comes right after a fall, which includes breathing and repositioning.
  - Whether the false-alarm rate during sleep is acceptable needs **a 20–30 min recording of lying still**.
- **Standing labels:** it is unclear whether the noisy standing recordings were truly still or included weight shifting. They need checking against the video.
- **Head turning:** only 6 s of data, so it cannot be evaluated.

## How to run
```
.venv\Scripts\python build_dataset.py            # data/ -> outputs/cache
.venv\Scripts\python run_experiments.py          # leave-one-date-out -> outputs/experiments
.venv\Scripts\python train.py                    # all data -> models/motion_state_v1.pkl
.venv\Scripts\python evaluate.py --data <folder> # e.g. the private holdout (same raw_csi/labels layout)
```

## Update: sleepwalking view (lying vs all motion), 2026-10-02
- The user watched the `sujin_stand_normal` videos. The person keeps turning their body, yet all 11 label files say `standing` from start to end. The standing labels are therefore unreliable.
- `run_experiments.py --scenario lying` re-scores with static = lying only. Standing is dropped from training and scoring.

Separation of lying from each motion, as AUC (threshold-free, leave-one-date-out):

| motion | AUC range over dates |
|---|---|
| walking | 0.987–1.000 |
| falling | 0.958–1.000 |
| raising arms | 0.961–0.991 |
| turning body | 0.917–0.999 |
| getting up | 0.925–0.957 |

Fixed threshold carried to the unseen date, results over all dates:

| lying FA target | min run | walk | fall | getting up | arms/turn | lying FA | walk segments | lying false events/h |
|---|---|---|---|---|---|---|---|---|
| 3% | 0.5 s | 0.99 | 0.96 | 0.59 | 0.79 | 0.11 | 47/47 | 195 |
| 1% | 2 s | 0.95 | 0.53 | 0.26 | 0.49 | 0.04 | 47/47 | 58 |
| 0.5% | 2 s | 0.89 | 0.39 | 0.13 | 0.21 | 0.01 | 46/47 | 32 |

Lying false alarms cluster in the two calm `lie_down_normal` recordings from 0624:
- **hoyeon:** about 80–120 s, 140–150 s and 160–170 s.
- **sujin:** periodic 3–5 s bursts.

These spans need a video check. They may be real small movements (tossing), which the user defines as static.

## Update: lie_down relabel from video (2026-10-02)
The user reviewed the video and relabelled the two 0624 `lie_down_normal` recordings. Originals are in `data/labels_original/`.

- **New labels:** `tossing`, `arm_movement`, `small_movement` (laughing/talking).
- They form a new `in_bed` class. It is excluded from training and from the static/dynamic score, and reported separately.

Results with the model trained without 0624:

| recording | in-bed segments caught | still-lying FA before → after relabel |
|---|---|---|
| sujin | 10/14 (missed 46–50, 141–145, 166–168, 179–180 s) | 19% → 4–5% |
| hoyeon | 9/9 | 29% → 12–20% |

- Remaining sujin false alarms: 82–84, 122–130, 174–176 s. These lie right next to tossing segments.
- Remaining hoyeon false alarms: 88–96, 105–120, 140–149 s. These need a video check.
- **Tossing vs walking:** AUC 0.99. Median log motion (`lag3_2s_med`): still lying -3.05, tossing -2.91, walking -2.08. So three levels (still / tossing / walking) look feasible.

## Update: hoyeon 1:28–1:36 set to ignore (2026-10-02)
- The user checked the video. Another team member was moving around nearby (chasing a mosquito), so 88–96 s of `hoyeon_lie_down_normal_01` is now `ignore`.
- **Two lie_down videos:**
  - Still lying reaches walking level for only 1.5 s. This is the end of the 2:11–2:19 body turn.
  - Tossing reaches walking level for 1.5 s (arm waving at 0:00–0:03).
  - Still vs tossing AUC 0.79 over all windows. Tossing is visible per segment but overlaps a lot per window.
- **All dates, walking vs lying (still + tossing):** walking-level threshold taken from the other dates.

| min run | walking segments | walking windows | tossing segments read as walking | still lying read as walking |
|---|---|---|---|---|
| 0.5 s | 47/47 | 95.3% | 3/22 | 0.8% |
| 2 s | 47/47 | 94.8% | 2/22 | 0.6% |
| 3 s | 46/47 | 94.5% | 1/22 | 0.2% |

- **Service note:** other people moving in the room produce walking-level CSI. The service must assume a single occupant, or treat this as a known false-alarm source.

## Update: still lying vs tossing, code-only improvement (2026-10-02)
Implemented in `motion_state/tossing.py`. It is per-night and offline, intended for the activity report.

**Steps:**
1. Compute a 1 s mean of the lag-0.5 s spectrum distance (short tossing gets blurred by the 2 s window).
2. Remove the night's own median. This takes out the difference in still-lying noise between people and rooms.
3. Apply centred 1.5 s smoothing.
4. Keep only flagged runs of at least 2 s.

Leave-one-recording-out results (train on sujin, test on hoyeon, and the reverse):

| method | window AUC (hoyeon / sujin) | balanced accuracy | tossing episodes caught | false episodes |
|---|---|---|---|---|
| previous (lag3, 2 s median) | 0.84 / 0.84 | 0.67 | 18/22 | 22 (4.8 per min of still lying) |
| 1 s lag5 + smoothing | 0.90 / 0.87 | 0.70 | 19/22 | 16 |
| + night-median normalisation | 0.90 / 0.87 | 0.82 | 19/22 | 11 |
| + min episode 2 s (final) | – | **0.83** | **19/22** | **6 (1.3/min)** |

- Combining many features with logistic regression did not help (AUC 0.79–0.81). It overfits to the one training person.
- Remaining false episodes:
  - sujin: 2:03–2:10
  - hoyeon: 1:45–2:00
- The detection at hoyeon 1:28–1:38 is the ignored span where another person was moving.
- Only 2 recordings (4.6 min of still lying) were available, so these numbers are a first estimate.
