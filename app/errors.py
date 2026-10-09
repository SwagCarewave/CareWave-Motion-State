from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.repositories import BackendUnavailable, RepositoryError
from app.services.csv_reader import CsvValidationError


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}


def _body(code: str, message: str, detail: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "detail": detail or {}}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.detail), status_code=exc.status)

    @app.exception_handler(CsvValidationError)
    async def csv_error(_: Request, exc: CsvValidationError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.detail), status_code=422)

    @app.exception_handler(BackendUnavailable)
    async def backend_down(_: Request, exc: BackendUnavailable) -> JSONResponse:
        return JSONResponse(_body(f"{exc.service}_unavailable", "저장소에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.",
                                  {"service": exc.service}), status_code=503)

    @app.exception_handler(RepositoryError)
    async def repository_error(_: Request, exc: RepositoryError) -> JSONResponse:
        return JSONResponse(_body("repository_error", "저장소 요청을 처리하지 못했습니다.", {"status": exc.status}),
                            status_code=502)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(_body(f"http_{exc.status_code}", str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(_body("invalid_request", "요청 형식이 올바르지 않습니다.", {"errors": exc.errors()}),
                            status_code=422)
