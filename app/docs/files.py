from __future__ import annotations

from app.schemas.system import ErrorOut

_NOT_FOUND = {404: {"model": ErrorOut, "description": "없는 파일 (file_not_found)"}}

UPLOAD = {
    "summary": "CSV·6대 녹화 업로드",
    "description": "두 종류를 받습니다.\n\n"
                   "- **ESP 4대 CSV** (`.csv`, 최대 50MB): 형식을 검사하고(필수 열: timestamp, rx, sub_0~sub_51) Storage에 저장합니다. "
                   "분석은 따로 시작합니다. 결과는 그래프·히트맵·사건입니다.\n"
                   "- **ESP 6대 녹화** (`.bin`, 수집 프로그램이 저장한 원본, 최대 300MB): 형식을 검사하고 **바로 분석을 시작**합니다"
                   "(응답의 `latest_analysis`). 지금 사용 중인 자세 모델로 스켈레톤을 계산하고, 결과는 재생바에 맞춘 스켈레톤 재생입니다. "
                   "원본은 분석이 성공하면 지웁니다(결과만 보관).\n\n"
                   "응답의 `kind`(csi4 | mesh6)로 화면을 나눕니다. 저장이나 등록이 실패하면 목록에 나오지 않습니다.",
    "responses": {
        413: {"model": ErrorOut, "description": "크기 초과 (file_too_large)"},
        422: {"model": ErrorOut, "description": "지원하지 않는 파일이거나 형식 오류 (unsupported_file, empty_file, invalid_format, "
                                                "missing_columns, no_valid_rows)"},
    },
}

LIST = {
    "summary": "등록된 파일 목록",
    "description": "등록된 CSV 목록입니다(최근 업로드 순). 파일마다 가장 최근 분석 상태가 함께 옵니다. "
                   "빈 배열이면 '등록된 CSV 파일이 없습니다'를 표시합니다.",
}

GET = {
    "summary": "등록된 파일 상세",
    "description": "파일 정보와 가장 최근 분석 상태를 반환합니다.",
    "responses": _NOT_FOUND,
}

DELETE = {
    "summary": "등록된 파일 삭제",
    "description": "원본(CSV 또는 남아 있는 6대 녹화), 분석 결과, 활동 사건, 보호자 확인, 진행 중인 재생을 함께 정리합니다. "
                   "진행 중인 분석은 멈춥니다. 중간에 실패하면 같은 요청을 다시 보내면 됩니다.",
    "responses": _NOT_FOUND,
}

FILE_ID_PARAM = "파일 ID"
UPLOAD_PARAM = "ESP 4대 CSV (최대 50MB) 또는 ESP 6대 녹화 .bin (최대 300MB)"
