from __future__ import annotations

from fastapi import APIRouter, File, Path, Request, Response, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.docs import analyses as analysis_docs
from app.docs import files as docs
from app.docs.openapi import TAG_CSV
from app.schemas.analyses import AnalysisStartOut
from app.schemas.files import FileOut
from app.services.analysis import AnalysisService
from app.services.files import FileService

router = APIRouter(prefix="/api/files", tags=[TAG_CSV])


def _files(request: Request) -> FileService:
    return request.app.state.files


def _analyses(request: Request) -> AnalysisService:
    return request.app.state.analyses


@router.post("", response_model=FileOut, status_code=status.HTTP_201_CREATED, **docs.UPLOAD)
async def upload_file(request: Request, file: UploadFile = File(description=docs.UPLOAD_PARAM)) -> FileOut:
    try:
        result = await run_in_threadpool(_files(request).upload, file.filename, file.file)
    finally:
        await file.close()
    return FileOut(**result)


@router.get("", response_model=list[FileOut], **docs.LIST)
def list_files(request: Request) -> list[FileOut]:
    return [FileOut(**f) for f in _files(request).list()]


@router.get("/{file_id}", response_model=FileOut, **docs.GET)
def get_file(request: Request, file_id: str = Path(description=docs.FILE_ID_PARAM)) -> FileOut:
    return FileOut(**_files(request).get(file_id))


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response, **docs.DELETE)
def delete_file(request: Request, file_id: str = Path(description=docs.FILE_ID_PARAM)) -> Response:
    _files(request).delete(file_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{file_id}/analyses", response_model=AnalysisStartOut, status_code=status.HTTP_201_CREATED,
             **analysis_docs.START)
def start_analysis(request: Request, response: Response,
                   file_id: str = Path(description=docs.FILE_ID_PARAM)) -> AnalysisStartOut:
    view, created = _analyses(request).start(file_id)
    if not created:
        response.status_code = status.HTTP_200_OK
    return AnalysisStartOut(**view, created=created)
