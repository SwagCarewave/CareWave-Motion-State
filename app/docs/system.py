from __future__ import annotations

HEALTH = {
    "summary": "서버 상태 확인",
    "description": "서버 동작, 최종 모델 해시 검사, DB·Storage 연결 상태를 확인합니다.",
}

MODEL = {
    "summary": "모델 정보 조회",
    "description": "판정에 사용 중인 최종 모델의 종류, 특징 수, 기준값, 해시를 반환합니다.",
}

CODES = {
    "summary": "정해진 값 목록",
    "description": "API에서 쓰는 정해진 값(코드)과 화면 표시용 한국어, 판단 기준을 한 번에 돌려줍니다. "
                   "이 목록으로 드롭다운·배지·문구를 만들면 됩니다.",
}
