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

## 6. 백엔드 API

```
.venv\Scripts\python scripts\migrate.py --apply
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
.venv\Scripts\python -m pytest
```

- `.env.example`을 `.env`로 복사하고 Supabase 값을 넣습니다. `CAREWAVE_STORAGE_BACKEND=memory`면 Supabase 없이 로컬에서 실행됩니다.
- `scripts\migrate.py`는 `supabase/migrations`의 SQL을 순서대로 적용합니다 (`--apply` 없이 실행하면 목록만 확인).

API 문서는 서버 실행 후 `http://localhost:8000/docs`(Swagger)에서 볼 수 있고, 설정 예시는 `.env.example`에 있습니다. 서버는 시작할 때 최종 모델(`models/lying_walking_v2.pkl`) 해시를 검사합니다.

### 실시간 CSI 보내기 (수집기 브리지)

수집기 출력(원시 CSV와 같은 열)을 API로 묶어서 보냅니다. 서버가 잠시 멈춰도 버퍼에 보관했다가 다시 보냅니다.

```
# PowerShell
<수집기> | .venv\Scripts\python scripts\collector_bridge.py --stdin
.venv\Scripts\python scripts\collector_bridge.py --replay data\raw_csi\test\yena_test_02_csi_raw.csv

# Git Bash
<수집기> | .venv/Scripts/python scripts/collector_bridge.py --stdin
.venv/Scripts/python scripts/collector_bridge.py --replay data/raw_csi/test/yena_test_02_csi_raw.csv
```

`--replay`는 저장된 CSV를 지금 시각으로 옮겨 실시간처럼 보냅니다(`--speed 4`로 배속). 실시간 그래프는 `/ws/sessions/{id}` WebSocket으로 받습니다.

### ESP32 CSI 받기

ESP32는 CSI raw 줄(`CSI_DATA,RX1,<mac>,<rssi>,...,[imag real imag real ...]`)을 UDP로 보냅니다.

- **서버로 바로 받기:** 서버가 켜지면 UDP `5005` 포트에서 받아, 취침 모드가 켜져 있는 세션에 넣습니다(`CAREWAVE_UDP_ENABLED`, `CAREWAVE_UDP_PORT`). 수신 상태는 `/api/health`의 `collector`에서 확인합니다. 배포할 때 UDP 5005 포트를 열어야 합니다.
- **보내는 곳 제한:** UDP는 인증이 없어서, 포트를 열면 누구나 패킷을 보낼 수 있습니다. ESP 기기(또는 공유기) IP를 `CAREWAVE_UDP_ALLOWED_SOURCES=192.168.0.21,192.168.0.22`처럼 적으면 그 IP에서 온 패킷만 받습니다. 비워 두면 모두 받습니다.
- **CSV로 저장하기:** 터미널에서 실행하면 학습 데이터와 같은 형식(`experiment_id,timestamp,label,rx,sub_0~sub_51`)으로 저장합니다. 같은 포트를 쓰므로 서버를 끄거나 `--port`를 바꿔서 실행합니다.

```
# PowerShell
.venv\Scripts\python scripts\collect_csi.py --label walk
.venv\Scripts\python scripts\collect_csi.py --label lie_down --duration 600 --out outputs\collected\lie_down_01.csv

# Git Bash
.venv/Scripts/python scripts/collect_csi.py --label walk
<시리얼 모니터 출력> | .venv/Scripts/python scripts/collect_csi.py --stdin --label walk
```

서브캐리어는 ESP32의 64개 중 빈 자리 12개(DC 1개, 가장자리 보호 대역 11개)를 위치로 빼서 항상 52개를 저장합니다.

## 7. 배포 (AWS EC2 + Docker)

서버는 UDP 수신과 실시간 세션을 메모리에서 처리하므로 **1대, 워커 1개**로 계속 켜 둡니다. `app`(FastAPI)과 `caddy`(HTTPS 인증서 자동 발급, WebSocket 포함 프록시) 두 컨테이너로 실행합니다.

```
# 로컬 확인 (https://localhost)
docker compose up --build

# 서버 (EC2, Ubuntu)
git clone https://github.com/SwagCarewave/CareWave-Motion-State.git && cd CareWave-Motion-State
cp .env.example .env    # STORAGE_BACKEND=supabase, CORS_ORIGINS=<프론트 주소>, CAREWAVE_DOMAIN=<API 도메인>, Supabase 값
docker compose run --rm app python scripts/migrate.py --apply
docker compose up -d --build
```

`main`에 push하면 `.github/workflows/deploy.yml`이 테스트를 돌리고, 통과하면 AWS SSM으로 EC2에서 `scripts/deploy.sh`를 실행합니다(최신 커밋으로 맞추고 다시 빌드한 뒤 `/api/health` 확인). GitHub Actions는 OIDC로 IAM 역할을 받아서 AWS 키를 저장하지 않고, SSH 포트도 열지 않습니다. 저장소 Variables에 `AWS_DEPLOY_ROLE_ARN`, `EC2_INSTANCE_ID`를 등록하기 전에는 배포 단계를 건너뜁니다. DB 마이그레이션은 자동으로 돌리지 않습니다.

- EC2 보안 그룹: TCP 80·443 열기, UDP 5005는 ESP32가 있는 곳의 공인 IP만, SSH 22는 내 IP만.
- `CAREWAVE_DOMAIN`의 DNS A 레코드를 EC2 탄력적 IP로 지정해야 인증서가 발급됩니다.
- AWS에서는 ESP32 패킷이 공유기의 **공인 IP**로 들어오므로 `CAREWAVE_UDP_ALLOWED_SOURCES`에도 공인 IP를 적습니다.
- 재시작하면 진행 중인 실시간 세션이 끊기므로 측정하지 않는 시간에 배포합니다.

### Supabase 일시 정지 방지

무료 플랜은 7일간 요청이 없으면 프로젝트가 멈춥니다. 서버가 켜져 있으면 5분마다 오래된 실시간 프레임을 지우는 요청을 보내므로 멈추지 않고, 서버가 꺼져 있을 때를 위해 `.github/workflows/supabase-keepalive.yml`이 이틀마다 Supabase에 조회 요청을 보냅니다. 저장소 Settings → Secrets and variables → Actions에 `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`를 등록하고, Actions 탭에서 `Run workflow`로 한 번 실행해 확인합니다.
