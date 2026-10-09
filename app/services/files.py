from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO, Callable

from app.config import Settings
from app.repositories import Repositories, RepositoryError
from app.services.csv_reader import CsvValidationError, load_csi_csv
from app.services.exceptions import ServiceError, not_found
from app.timeutil import from_iso, to_iso

log = logging.getLogger("carewave.files")

CHUNK = 1024 * 1024
ACTIVE_ANALYSIS = ("queued", "running")


def purge_analyses(repos: Repositories, analyses: list[dict]) -> None:
    if not analyses:
        return
    results = [a["result_path"] for a in analyses if a.get("result_path")]
    if results:
        repos.results_store.delete(results)
    ids = [a["id"] for a in analyses]
    events = repos.activity_events.list({"analysis_id__in": ids})
    if events:
        repos.guardian_confirmations.delete_where({"event_id__in": [e["id"] for e in events]})
        repos.activity_events.delete_where({"analysis_id__in": ids})
    repos.analyses.delete_where({"id__in": ids})


def _analysis_view(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "status": row["status"],
        "progress": row.get("progress") or 0.0,
        "event_count": row.get("event_count"),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "finished_at": from_iso(row.get("finished_at")),
    }


class FileService:
    def __init__(self, settings: Settings, repos: Repositories):
        self.settings = settings
        self.repos = repos
        self.on_delete: list[Callable[[dict, list[dict]], None]] = []

    def upload(self, filename: str | None, source: BinaryIO) -> dict:
        name = Path(filename or "").name.strip()
        if not name.lower().endswith(".csv"):
            raise ServiceError(422, "unsupported_file", "CSV 파일만 올릴 수 있습니다.", {"filename": name})
        fd, tmp_name = tempfile.mkstemp(suffix=".csv")
        tmp = Path(tmp_name)
        try:
            try:
                size, digest = self._copy_limited(source, fd)
            finally:
                os.close(fd)
            if size == 0:
                raise CsvValidationError("empty_file", "빈 파일입니다. 다른 파일을 선택해 주세요.")
            table = load_csi_csv(tmp)
            data = tmp.read_bytes()
        finally:
            tmp.unlink(missing_ok=True)
        p = table.preview
        file_id = str(uuid.uuid4())
        row = self.repos.csv_files.insert({
            "id": file_id,
            "filename": name,
            "storage_path": f"{file_id}.csv",
            "size_bytes": size,
            "sha256": digest,
            "status": "uploading",
            "rows": p.rows,
            "packets": p.packets,
            "incomplete_rows": p.incomplete_rows,
            "receivers": p.receivers,
            "packets_by_rx": p.packets_by_rx,
            "record_start": to_iso(p.start_ts),
            "record_end": to_iso(p.end_ts),
        })
        try:
            self.repos.csv_store.put(row["storage_path"], data, "text/csv")
        except Exception:
            log.exception("storage upload failed for file %s", file_id)
            self._mark_failed(file_id, "storage_upload_failed")
            raise
        try:
            row = self.repos.csv_files.update(file_id, {"status": "uploaded"}) or row
        except Exception:
            log.exception("could not mark file %s uploaded", file_id)
            self._discard(row)
            raise
        return self.view(row, None)

    def list(self) -> list[dict]:
        rows = self.repos.csv_files.list({"status": "uploaded"}, order_by="created_at", desc=True)
        latest = self._latest_analyses([r["id"] for r in rows])
        return [self.view(r, latest.get(r["id"])) for r in rows]

    def get(self, file_id: str) -> dict:
        row = self.row(file_id)
        return self.view(row, self._latest_analyses([file_id]).get(file_id))

    def row(self, file_id: str) -> dict:
        try:
            row = self.repos.csv_files.get(file_id)
        except RepositoryError as exc:
            if exc.status in (400, 404):
                raise not_found("file_not_found", "등록된 파일을 찾을 수 없습니다.", "file_id", file_id) from exc
            raise
        if row is None or row["status"] != "uploaded":
            raise not_found("file_not_found", "등록된 파일을 찾을 수 없습니다.", "file_id", file_id)
        return row

    def delete(self, file_id: str) -> None:
        row = self.row(file_id)
        analyses = self.repos.analyses.list({"file_id": file_id})
        for hook in self.on_delete:
            hook(row, analyses)
        self.repos.csv_store.delete([row["storage_path"]])
        purge_analyses(self.repos, self.repos.analyses.list({"file_id": file_id}))
        self.repos.csv_files.delete(file_id)

    def recover(self) -> None:
        for row in self.repos.csv_files.list({"status": "uploading"}):
            self._mark_failed(row["id"], "interrupted")

    def view(self, row: dict, analysis: dict | None) -> dict:
        start, end = from_iso(row["record_start"]), from_iso(row["record_end"])
        return {
            "id": row["id"],
            "filename": row["filename"],
            "size_bytes": row["size_bytes"],
            "rows": row["rows"],
            "packets": row["packets"],
            "incomplete_rows": row.get("incomplete_rows") or 0,
            "receivers": row.get("receivers") or [],
            "packets_by_rx": row.get("packets_by_rx") or {},
            "record_start": start,
            "record_end": end,
            "duration_sec": round(end - start, 3),
            "uploaded_at": from_iso(row.get("created_at")),
            "latest_analysis": _analysis_view(analysis),
        }

    def _latest_analyses(self, file_ids: list[str]) -> dict[str, dict]:
        if not file_ids:
            return {}
        latest: dict[str, dict] = {}
        for a in self.repos.analyses.list({"file_id__in": file_ids}, order_by="created_at", desc=True):
            latest.setdefault(a["file_id"], a)
        return latest

    def _copy_limited(self, source: BinaryIO, fd: int) -> tuple[int, str]:
        size, digest = 0, hashlib.sha256()
        limit = self.settings.max_upload_bytes
        while True:
            chunk = source.read(CHUNK)
            if not chunk:
                return size, digest.hexdigest()
            size += len(chunk)
            if size > limit:
                raise ServiceError(413, "file_too_large", f"파일이 너무 큽니다. 최대 {self.settings.max_upload_mb}MB까지 올릴 수 있습니다.",
                                   {"max_bytes": limit})
            digest.update(chunk)
            os.write(fd, chunk)

    def _mark_failed(self, file_id: str, code: str) -> None:
        try:
            self.repos.csv_files.update(file_id, {"status": "upload_failed", "error_code": code,
                                                  "error_message": "파일을 저장하지 못했습니다."})
        except Exception:
            log.exception("could not mark file %s as failed", file_id)

    def _discard(self, row: dict) -> None:
        try:
            self.repos.csv_store.delete([row["storage_path"]])
            self.repos.csv_files.delete(row["id"])
        except Exception:
            log.exception("could not discard half-uploaded file %s", row["id"])
