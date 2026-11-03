from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.sessions import SignalsOut

POSE_EXAMPLE = {
    "type": "pose",
    "ts": 1793532990.4,
    "model": "carewave_v3_upper_20261102",
    "points": [{"name": "head_nose", "x": 0.512, "y": 0.231},
               {"name": "left_shoulder", "x": 0.571, "y": 0.334},
               {"name": "right_shoulder", "x": 0.455, "y": 0.331}],
    "smoothed": [{"name": "head_nose", "x": 0.51, "y": 0.233},
                 {"name": "left_shoulder", "x": 0.569, "y": 0.335},
                 {"name": "right_shoulder", "x": 0.457, "y": 0.332}],
    "action": {"code": "stand", "label": "서 있음", "score": 0.93},
    "probabilities": {"stand": 0.93, "walk": 0.04},
    "posture": {"code": "upright", "label": "서 있는 자세", "scores": {"upright": 0.97, "low": 0.02, "lying": 0.01}},
    "activity_score": 0.03,
    "direction": "up",
    "direction_ko": "상",
    "fall_alarm": False,
}


class PointOut(BaseModel):
    name: str = Field(description="점 이름 (head_nose: 코, left_shoulder: 왼쪽 어깨, right_shoulder: 오른쪽 어깨)")
    x: float = Field(description="영상 가로 기준 0~1 좌표 (왼쪽 0)")
    y: float = Field(description="영상 세로 기준 0~1 좌표 (위 0)")


class ActionOut(BaseModel):
    code: str = Field(description="가장 확률이 높은 행동 코드")
    label: str = Field(description="행동 한국어 이름")
    score: float = Field(description="그 행동의 확률 (0~1, 세 모델 평균)")


class PostureOut(BaseModel):
    code: str = Field(description="아바타 자세: upright | low | lying | uncertain(몸통 숨기고 점만 표시)")
    label: str = Field(description="자세 한국어 이름")
    scores: dict[str, float] = Field(description="자세 묶음별 확률 합")


class PoseFrameOut(BaseModel):
    type: str = Field(description="항상 pose")
    ts: float = Field(description="추정이 설명하는 시각 (Unix 초, 서버 수신 시각 기준)")
    model: str = Field(description="추정한 모델 이름")
    points: list[PointOut] = Field(description="모델 원출력 좌표. 점 개수·이름은 모델마다 다름 (snapshot의 model.points, model.skeleton 참고)")
    smoothed: list[PointOut] = Field(description="화면 표시용 평활화 좌표")
    action: ActionOut
    probabilities: dict[str, float] = Field(description="행동별 확률 (모델의 classes)")
    posture: PostureOut
    activity_score: float = Field(description="움직임 역동성 0~1. 행동 확률에 행동별 역동성(manifest game.intensity)을 곱해 더한 값. "
                                              "가만히 있으면 0, 조심히 움직이면 낮고 역동적으로 움직이면 높음")
    direction: str | None = Field(description="미로 게임 방향. activity_score 0~0.2: up, 0.2~0.8: left, 0.8~1.0: right")
    direction_ko: str | None = Field(description="상 | 좌 | 우")
    fall_alarm: bool = Field(description="넘어짐 확률이 0.7을 넘는 상태가 0.4초 이어지면 true")
    links_seen: int | None = Field(default=None, description="13관절 모델만: 이번 0.05초에 들어온 선 수")
    pixels: dict | None = Field(default=None, description="13관절 모델만: 좌표 기준 영상 크기 (width, height). 픽셀 = x·width, y·height")


class LinkOut(BaseModel):
    link: str = Field(description="선 이름 n{받은 ESP}_p{보낸 ESP} (원본 패킷의 노드 필드 순서)")
    from_: int = Field(alias="from", description="보낸 ESP 번호 (1~6)")
    to: int = Field(description="받는 ESP 번호 (1~6)")
    status: str = Field(description="ok | weak | lost | none")
    status_ko: str = Field(description="정상 | 신호 부족 | 연결 끊김 | 미연결")
    rate_hz: float = Field(description="최근 2초 초당 레코드 수 (정상 약 50)")
    age_sec: float | None = Field(description="마지막 레코드 후 지난 초")

    model_config = {"populate_by_name": True}


class CalibrationOut(BaseModel):
    required: bool = Field(description="이 모델이 시작 보정을 쓰는지")
    active: bool = Field(description="지금 보정 중 (사람이 서서 가만히 있어야 함, 추정 없음)")
    progress: float = Field(description="보정 진행 0~1")
    seconds: float = Field(description="보정에 걸리는 시간(초)")


class PoseStatusOut(BaseModel):
    model: dict = Field(description="사용 중인 모델 정보: 이름, 선 순서, 점·행동 이름, 표시·게임 규칙, 스켈레톤 연결선")
    latest: PoseFrameOut | None = Field(description="가장 최근 추정")
    counters: dict[str, int] = Field(description="받은 레코드 수 등 누적 카운터")
    predictions: int = Field(description="이 모델로 낸 추정 수")
    links: list[LinkOut] = Field(description="30개 선 상태")
    links_ok: int = Field(description="정상인 선 수")
    links_total: int = Field(description="전체 선 수 (30)")
    ready: bool = Field(description="30개 선이 모두 정상이면 true")
    valid_frame_ratio: float | None = Field(description="입력 프레임 중 30개 선이 모두 들어온 비율")
    calibration: CalibrationOut = Field(description="시작 보정 상태")
    seconds_since_last_record: float | None = Field(description="마지막 레코드 후 지난 초")


class PoseLatestOut(BaseModel):
    latest: PoseFrameOut | None = Field(json_schema_extra={"example": POSE_EXAMPLE})


class PoseModelItem(BaseModel):
    name: str = Field(description="모델 폴더 이름")
    title: str = Field(description="설명")
    type: str | None = Field(description="모델 종류: upper3_gru (머리·어깨, 앙상블) | csipose_tcn (13관절, 시작 보정)")
    points: int = Field(description="추정하는 점 개수")
    classes: list[str] = Field(description="행동 종류")
    active: bool = Field(description="현재 사용 중")


class PoseSwitchIn(BaseModel):
    name: str = Field(description="바꿀 모델 폴더 이름", examples=["carewave_v3_upper_20261102"])


class PoseSignalsOut(SignalsOut):
    session_id: str | None = Field(default=None, description="항상 null (6대는 세션 없음)")
