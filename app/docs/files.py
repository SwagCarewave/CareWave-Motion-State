from __future__ import annotations

from app.schemas.system import ErrorOut

_NOT_FOUND = {404: {"model": ErrorOut, "description": "없는 파일 (file_not_found)"}}

UPLOAD = {
    "summary": "CSV 업로드",
    "description": "CSV를 올리면 형식을 검사하고(필수 열: timestamp, rx, sub_0~sub_51) Storage에 저장한 뒤 등록합니다. "
                   "응답으로 '업로드 파일 확인' 화면에 쓸 기록 시간과 수신기를 돌려줍니다. "
                   "Storage 저장이나 DB 등록이 실패하면 등록되지 않으며 목록에도 나오지 않습니다. 분석은 따로 시작합니다.",
    "responses": {
        413: {"model": ErrorOut, "description": "50MB 초과 (file_too_large)"},
        422: {"model": ErrorOut, "description": "CSV가 아니거나 형식 오류 (unsupported_file, empty_file, invalid_format, "
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
    "description": "원본 CSV, 분석 결과, 활동 사건, 보호자 확인, 진행 중인 재생을 함께 정리합니다. "
                   "진행 중인 분석은 멈춥니다. 중간에 실패하면 같은 요청을 다시 보내면 됩니다.",
    "responses": _NOT_FOUND,
}

FILE_ID_PARAM = "파일 ID"
UPLOAD_PARAM = "CSV 파일 (최대 50MB)"
