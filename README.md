# CareWave Motion State

Wi-Fi CSI 원시 데이터만으로 **눕기(가만히 누움 + 뒤척임)와 걷기 수준의 큰 움직임**을 0.5초마다 구분하고, 걷기 수준 움직임이 3초 이상 이어지면 보호자 알림을 만드는 모델과 실시간 엔진입니다.

자세한 설명은 `docs/model_v2.md`, `docs/service_v2.md`에 있습니다.

## 1. 환경 설치 (Windows, Python 3.10)

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

`requirements.txt`의 버전은 결과를 낸 환경과 같게 고정되어 있습니다. 버전이 다르면 숫자가 미세하게 달라지거나 저장된 모델(`.pkl`)이 열리지 않을 수 있습니다.

## 2. 결과 재현 (순서대로 실행)

| 순서 | 명령 | 하는 일 | 시간(참고) |
|---|---|---|---|
| 1 | `.venv\Scripts\python build_v2.py --lookahead 1 --tossing static --standing exclude --walking-only` | CSI 전처리, 특징 계산 (`outputs/v2/`) | 약 5분 |
| 2 | `.venv\Scripts\python train_v2.py --table outputs/v2/windows_L1_toss-static_stand-exclude_walkonly.csv --group date --out outputs/v2/toss-static_stand-exclude_walkonly` | 날짜별 중첩 교차검증 | 약 10분 |
| 3 | `.venv\Scripts\python train_v2.py --table outputs/v2/windows_L1_toss-static_stand-exclude_walkonly.csv --group person --out outputs/v2/toss-static_stand-exclude_walkonly` | 사람별 중첩 교차검증 | 약 10분 |
| 4 | `.venv\Scripts\python train_v2.py --table outputs/v2/windows_L1_toss-static_stand-exclude_walkonly.csv --group date --out outputs/v2/walk_final --save-final models/lying_walking_v2.pkl` | 최종 모델 학습·저장 | 약 10분 |
| 5 | `.venv\Scripts\python eval_service.py` | 알림 단위 평가 (실시간 재생) | 약 10분 |

**기대 결과**

| 단계 | 항목 | 값 |
|---|---|---|
| 2 | 날짜별 정확도 / Macro-F1 | 0.948 / 0.947 |
| 3 | 사람별 정확도 | 0.957 |
| 4 | 최종 모델 | 로지스틱 회귀, 특징 60개, 기준값 0.500 (CV 균형 정확도 0.957) |
| 5 | 3초 규칙 | 준비 후 시작한 걷기 11/11 알림, 누움·뒤척임 중 오알림 1회 |

## 3. 실행

```
.venv\Scripts\python run_monitor.py --replay data\raw_csi\test\yena_test_02_csi_raw.csv
<수집기> | .venv\Scripts\python run_monitor.py --stdin
```

`models/lying_walking_v2.pkl`이 저장소에 포함되어 있어, 2번(재현) 없이 바로 실행할 수 있습니다.

## 4. Holdout 평가

Holdout 녹화 10개는 저장소에 없습니다(평가용으로 따로 보관). 받으신 경우:

```
.venv\Scripts\python evaluate_v2.py --data <holdout 폴더>
```

기대 결과: 정확도 0.982, 걷기 알림 4/4, 누워 있을 때 오알림 0회.

## 5. 폴더

| 경로 | 내용 |
|---|---|
| `data/raw_csi/` | 원시 CSI 107개 (개발용) |
| `data/labels/` | 라벨 (영상으로 다시 확인해 수정한 버전) |
| `data/labels_original/` | 수정 전 원본 라벨 9개 |
| `motion_state/` | 전처리, 특징, 실시간 감지기·모니터 |
| `models/` | 학습된 모델 |
| `outputs/` | 실행하면 생기는 캐시·결과 (저장소에 없음) |
