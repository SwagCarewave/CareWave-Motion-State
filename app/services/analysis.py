from __future__ import annotations

import gzip
import json
import logging
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from app.config import Settings
from app.repositories import BackendUnavailable, ObjectNotFound, Repositories, RepositoryError
from app.services.csv_reader import CsvValidationError, load_csi_csv
from app.services.engine import build_engine, run_table
from app.services.exceptions import ServiceError, not_found
from app.services.files import ACTIVE_ANALYSIS, FileService, purge_analyses
from app.services.series import build_series, columns_to_points, points_to_columns
from app.timeutil import from_iso, to_iso

log = logging.getLogger("carewave.analysis")

RESULT_VERSION = 1
FAIL_MESSAGES = {
    "source_missing": "원본 CSV를 찾을 수 없습니다. 파일을 다시 올려 주세요.",
    "storage_unavailable": "저장소에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.",
    "interrupted": "서버가 다시 시작되어 분석이 중단되었습니다. 다시 시도해 주세요.",
    "cancelled": "파일이 삭제되어 분석을 멈췄습니다.",
    "analysis_error": "CSV 분석 중 오류가 발생했습니다. 같은 파일로 다시 시도하거나 다른 파일을 선택해 주세요.",
}


class AnalysisCancelled(Exception):
    pass


class AnalysisService:
    def __init__(self, settings: Settings, repos: Repositories, files: FileService,
                 clock: Callable[[], float] = time.time):
        self.settings = settings
        self.repos = repos
        self.files = files
        self.clock = clock
        self.executor = ThreadPoolExecutor(max_workers=settings.analysis_workers, thread_name_prefix="analysis")
        self.cancelled: set[str] = set()
        self.cache: OrderedDict[str, dict] = OrderedDict()
        self.lock = threading.RLock()
        self.finalize_lock = threading.Lock()
        self.on_retire: list[Callable[[set[str]], None]] = []
        files.on_delete.append(self._on_file_delete)

    def recover(self) -> None:
        for row in self.repos.analyses.list({"status__in": list(ACTIVE_ANALYSIS)}):
            self._fail(row["id"], "interrupted")

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def start(self, file_id: str) -> tuple[dict, bool]:
        with self.lock:
            file_row = self.files.row(file_id)
            active = self.repos.analyses.list({"file_id": file_id, "status__in": list(ACTIVE_ANALYSIS)},
                                              order_by="created_at", desc=True, limit=1)
            if active:
                return self.view(active[0]), False
            attempt = len(self.repos.analyses.list({"file_id": file_id})) + 1
            row = self.repos.analyses.insert({"id": str(uuid.uuid4()), "file_id": file_id, "status": "queued",
                                              "progress": 0.0, "attempt": attempt})
        self.executor.submit(self._run, row["id"], file_row)
        return self.view(row), True

    def get(self, analysis_id: str) -> dict:
        return self.view(self.row(analysis_id))

    def row(self, analysis_id: str) -> dict:
        try:
            row = self.repos.analyses.get(analysis_id)
        except RepositoryError as exc:
            if exc.status in (400, 404):
                raise not_found("analysis_not_found", "분석을 찾을 수 없습니다.", "analysis_id", analysis_id) from exc
            raise
        if row is None:
            raise not_found("analysis_not_found", "분석을 찾을 수 없습니다.", "analysis_id", analysis_id)
        return row

    def ready_row(self, analysis_id: str) -> dict:
        row = self.row(analysis_id)
        if row["status"] != "succeeded":
            raise ServiceError(409, "analysis_not_ready", "분석이 아직 끝나지 않았습니다.",
                               {"analysis_id": analysis_id, "status": row["status"]})
        return row

    def view(self, row: dict) -> dict:
        summary = row.get("summary") or {}
        return {
            "id": row["id"],
            "file_id": row["file_id"],
            "status": row["status"],
            "progress": round(float(row.get("progress") or 0.0), 4),
            "attempt": row.get("attempt") or 1,
            "frame_count": row.get("frame_count"),
            "event_count": row.get("event_count"),
            "record_start": summary.get("record_start"),
            "record_end": summary.get("record_end"),
            "duration_sec": summary.get("duration_sec"),
            "error_code": row.get("error_code"),
            "error_message": row.get("error_message"),
            "started_at": from_iso(row.get("started_at")),
            "finished_at": from_iso(row.get("finished_at")),
            "created_at": from_iso(row.get("created_at")),
        }

    def points(self, analysis_id: str) -> tuple[dict, list[dict]]:
        with self.lock:
            cached = self.cache.get(analysis_id)
            if cached is not None:
                self.cache.move_to_end(analysis_id)
                return cached["meta"], cached["points"]
        row = self.ready_row(analysis_id)
        try:
            raw = self.repos.results_store.get(row["result_path"])
        except ObjectNotFound as exc:
            raise ServiceError(410, "result_missing", "분석 결과를 찾을 수 없습니다. 다시 분석해 주세요.",
                               {"analysis_id": analysis_id}) from exc
        data = json.loads(gzip.decompress(raw))
        meta = {k: v for k, v in data.items() if k != "columns"}
        points = columns_to_points(data["columns"])
        with self.lock:
            self.cache[analysis_id] = {"meta": meta, "points": points}
            while len(self.cache) > self.settings.results_cache_size:
                self.cache.popitem(last=False)
        return meta, points

    def markers(self, analysis_id: str) -> list[dict]:
        rows = self.repos.activity_events.list({"analysis_id": analysis_id}, order_by="event_no")
        found = {}
        if rows:
            found = {c["event_id"]: c["result"] for c in
                     self.repos.guardian_confirmations.list({"event_id__in": [r["id"] for r in rows]})}
        return [{"id": r["event_no"], "uuid": r["id"], "start_ts": from_iso(r["started_at"]),
                 "alert_ts": from_iso(r["alerted_at"]), "end_ts": from_iso(r.get("ended_at")),
                 "duration_sec": r.get("duration_sec") or 0.0, "ongoing": False,
                 "guardian_result": found.get(r["id"])} for r in rows]

    def signals(self, analysis_id: str, window_sec: float | None = None, from_ts: float | None = None,
                to_ts: float | None = None, max_points: int = 1200, heatmap: bool = True) -> dict:
        meta, points = self.points(analysis_id)
        first = points[0]["ts"] if points else meta["record_start"]
        last = points[-1]["ts"] if points else meta["record_end"]
        end = to_ts if to_ts is not None else last
        if from_ts is not None:
            start = from_ts
        elif window_sec is not None:
            start = end - window_sec
        else:
            start = first
        series = build_series(points, self.markers(analysis_id), start, end, max_points, heatmap,
                              meta.get("threshold"), meta.get("step_sec", self.settings.frame_step_sec),
                              self.settings.event_burst_sec)
        return {"analysis_id": analysis_id, "file_id": meta.get("file_id"), **series}

    def _run(self, analysis_id: str, file_row: dict) -> None:
        result_path = f"{analysis_id}.json.gz"
        try:
            self._update(analysis_id, {"status": "running", "started_at": to_iso(self.clock()), "progress": 0.0})
            try:
                raw = self.repos.csv_store.get(file_row["storage_path"])
            except ObjectNotFound:
                self._fail(analysis_id, "source_missing")
                return
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "source.csv"
                path.write_bytes(raw)
                table = load_csi_csv(path)
            engine = build_engine(self.settings, origin_ts=table.origin_ts)
            duration = max(float(table.offset[-1]), 1e-9)
            progress = {"value": 0.0, "wall": 0.0}
            alerts: dict[int, str] = {}

            def on_frames(batch, t):
                if analysis_id in self.cancelled:
                    raise AnalysisCancelled()
                for f in batch:
                    if f.alert:
                        alerts[f.event_id] = f.alert
                value = min(0.99, t / duration)
                if value - progress["value"] >= self.settings.analysis_progress_step:
                    progress["value"] = value
                    self._update(analysis_id, {"progress": round(value, 4)})

            frames = run_table(engine, table, on_frames)
            points = [{"ts": f.ts, "state": f.state, "motion_index": f.motion_index,
                       "activity_score": f.activity_score, "event_id": f.event_id, "signal_ok": f.signal_ok,
                       "heatmap": f.heatmap} for f in frames]
            meta = {"version": RESULT_VERSION, "analysis_id": analysis_id, "file_id": file_row["id"],
                    "step_sec": self.settings.frame_step_sec, "threshold": engine.threshold,
                    "record_start": table.preview.start_ts, "record_end": table.preview.end_ts}
            payload = gzip.compress(json.dumps({**meta, "columns": points_to_columns(points)},
                                               ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            events = engine.events()
            event_rows = [{"id": str(uuid.uuid4()), "session_id": None, "analysis_id": analysis_id,
                           "event_no": e["id"], "started_at": to_iso(e["start_ts"]),
                           "alerted_at": to_iso(e["alert_ts"]),
                           "ended_at": None if e["end_ts"] is None else to_iso(e["end_ts"]),
                           "duration_sec": e["duration_sec"], "alert_message": alerts.get(e["id"])} for e in events]
            summary = {"record_start": table.preview.start_ts, "record_end": table.preview.end_ts,
                       "duration_sec": table.preview.duration_sec}
            with self.finalize_lock:
                if analysis_id in self.cancelled:
                    raise AnalysisCancelled()
                self.repos.results_store.put(result_path, payload, "application/gzip")
                self.repos.activity_events.insert_many(event_rows)
                self._carry_confirmations(file_row["id"], analysis_id, event_rows)
                updated = self.repos.analyses.update(analysis_id, {
                    "status": "succeeded", "progress": 1.0, "result_path": result_path, "frame_count": len(frames),
                    "event_count": len(events), "summary": summary, "error_code": None, "error_message": None,
                    "finished_at": to_iso(self.clock())})
                if updated is None:
                    raise AnalysisCancelled()
                with self.lock:
                    self.cache[analysis_id] = {"meta": meta, "points": points}
                    while len(self.cache) > self.settings.results_cache_size:
                        self.cache.popitem(last=False)
            self._retire_previous(file_row["id"], analysis_id)
        except AnalysisCancelled:
            with self.finalize_lock:
                self._cleanup_partial(analysis_id, result_path)
                with self.lock:
                    self.cache.pop(analysis_id, None)
            self._fail(analysis_id, "cancelled")
        except CsvValidationError as exc:
            self._fail(analysis_id, exc.code, exc.message)
        except BackendUnavailable:
            log.exception("analysis %s: storage unavailable", analysis_id)
            self._cleanup_partial(analysis_id, result_path)
            self._fail(analysis_id, "storage_unavailable")
        except Exception:
            log.exception("analysis %s failed", analysis_id)
            self._cleanup_partial(analysis_id, result_path)
            self._fail(analysis_id, "analysis_error")
        finally:
            self.cancelled.discard(analysis_id)

    def _carry_confirmations(self, file_id: str, analysis_id: str, event_rows: list[dict]) -> None:
        previous = [a for a in self.repos.analyses.list({"file_id": file_id, "status": "succeeded"})
                    if a["id"] != analysis_id]
        if not previous or not event_rows:
            return
        old_events = self.repos.activity_events.list({"analysis_id__in": [a["id"] for a in previous]})
        if not old_events:
            return
        confirmations = {c["event_id"]: c for c in
                         self.repos.guardian_confirmations.list({"event_id__in": [e["id"] for e in old_events]})}
        by_no = {}
        for e in sorted(old_events, key=lambda e: e.get("created_at") or ""):
            if e["id"] in confirmations:
                by_no[e["event_no"]] = confirmations[e["id"]]
        for row in event_rows:
            c = by_no.get(row["event_no"])
            if c is not None:
                self.repos.guardian_confirmations.upsert(
                    {"event_id": row["id"], "result": c["result"], "confirmed_at": c["confirmed_at"]}, ("event_id",))

    def _retire_previous(self, file_id: str, analysis_id: str) -> None:
        old = [a for a in self.repos.analyses.list({"file_id": file_id})
               if a["id"] != analysis_id and a["status"] not in ACTIVE_ANALYSIS]
        try:
            purge_analyses(self.repos, old)
        except Exception:
            log.exception("could not retire previous analyses of file %s", file_id)
        with self.lock:
            for a in old:
                self.cache.pop(a["id"], None)
        if old:
            for hook in self.on_retire:
                hook({a["id"] for a in old})

    def _cleanup_partial(self, analysis_id: str, result_path: str) -> None:
        try:
            self.repos.activity_events.delete_where({"analysis_id": analysis_id})
            self.repos.results_store.delete([result_path])
        except Exception:
            log.exception("could not clean partial results of analysis %s", analysis_id)

    def _fail(self, analysis_id: str, code: str, message: str | None = None) -> None:
        self._update(analysis_id, {"status": "failed", "error_code": code,
                                   "error_message": message or FAIL_MESSAGES.get(code, FAIL_MESSAGES["analysis_error"]),
                                   "finished_at": to_iso(self.clock())})

    def _update(self, analysis_id: str, changes: dict) -> None:
        for attempt in range(3):
            try:
                self.repos.analyses.update(analysis_id, changes)
                return
            except Exception:
                if attempt == 2:
                    log.exception("could not update analysis %s", analysis_id)
                    return
                time.sleep(0.5 * (attempt + 1))

    def _on_file_delete(self, file_row: dict, analyses: list[dict]) -> None:
        with self.finalize_lock:
            for a in analyses:
                if a["status"] in ACTIVE_ANALYSIS:
                    self.cancelled.add(a["id"])
                with self.lock:
                    self.cache.pop(a["id"], None)
