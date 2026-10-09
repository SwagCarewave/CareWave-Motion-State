from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import ObjectNotFound, Repositories, split_filter


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _match(row: dict, filters: dict[str, Any] | None) -> bool:
    for key, value in (filters or {}).items():
        field, op = split_filter(key)
        current = row.get(field)
        if op == "is" or (op == "eq" and value is None):
            if current is not value:
                return False
        elif op == "eq":
            if current != value:
                return False
        elif op == "in":
            if current not in value:
                return False
        elif current is None:
            return False
        elif op == "gt" and not current > value:
            return False
        elif op == "gte" and not current >= value:
            return False
        elif op == "lt" and not current < value:
            return False
        elif op == "lte" and not current <= value:
            return False
    return True


class MemoryTable:
    def __init__(self, name: str):
        self.name = name
        self._rows: dict[str, dict] = {}
        self._lock = threading.RLock()

    def insert(self, row: dict) -> dict:
        return self.insert_many([row])[0]

    def insert_many(self, rows: list[dict]) -> list[dict]:
        with self._lock:
            out = []
            for row in rows:
                item = copy.deepcopy(row)
                item.setdefault("id", str(uuid.uuid4()))
                item.setdefault("created_at", _now())
                if item["id"] not in self._rows:
                    self._rows[item["id"]] = item
                out.append(copy.deepcopy(self._rows[item["id"]]))
            return out

    def upsert(self, row: dict, on_conflict: tuple[str, ...]) -> dict:
        with self._lock:
            for existing in self._rows.values():
                if all(existing.get(k) == row.get(k) for k in on_conflict):
                    return self.update(existing["id"], {k: v for k, v in row.items() if k != "id"})
            return self.insert(row)

    def get(self, row_id: str) -> dict | None:
        with self._lock:
            row = self._rows.get(row_id)
            return None if row is None else copy.deepcopy(row)

    def list(self, filters: dict[str, Any] | None = None, order_by: str | None = None, desc: bool = False,
             limit: int | None = None, offset: int = 0) -> list[dict]:
        with self._lock:
            rows = [r for r in self._rows.values() if _match(r, filters)]
            if order_by is not None:
                rows.sort(key=lambda r: (r.get(order_by) is None, r.get(order_by)), reverse=desc)
            rows = rows[offset:]
            if limit is not None:
                rows = rows[:limit]
            return copy.deepcopy(rows)

    def update(self, row_id: str, changes: dict[str, Any]) -> dict | None:
        with self._lock:
            row = self._rows.get(row_id)
            if row is None:
                return None
            row.update(copy.deepcopy(changes))
            row["updated_at"] = _now()
            return copy.deepcopy(row)

    def delete(self, row_id: str) -> bool:
        with self._lock:
            return self._rows.pop(row_id, None) is not None

    def delete_where(self, filters: dict[str, Any]) -> int:
        with self._lock:
            doomed = [k for k, r in self._rows.items() if _match(r, filters)]
            for k in doomed:
                del self._rows[k]
            return len(doomed)


class LocalObjectStore:
    def __init__(self, root: Path, bucket: str):
        self.bucket = bucket
        self.root = Path(root) / bucket
        self._lock = threading.RLock()

    def _path(self, path: str) -> Path:
        target = (self.root / path).resolve()
        if self.root.resolve() not in target.parents:
            raise ValueError(f"invalid object path: {path}")
        return target

    def put(self, path: str, data: bytes, content_type: str) -> None:
        target = self._path(path)
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(target)

    def get(self, path: str) -> bytes:
        target = self._path(path)
        if not target.is_file():
            raise ObjectNotFound(path)
        return target.read_bytes()

    def delete(self, paths: list[str]) -> None:
        with self._lock:
            for path in paths:
                self._path(path).unlink(missing_ok=True)

    def exists(self, path: str) -> bool:
        return self._path(path).is_file()


def memory_repositories(storage_root: Path, csv_bucket: str = "csv-uploads",
                        results_bucket: str = "analysis-results") -> Repositories:
    return Repositories(
        backend="memory",
        sessions=MemoryTable("sessions"),
        device_status=MemoryTable("device_status"),
        signal_frames=MemoryTable("signal_frames"),
        activity_events=MemoryTable("activity_events"),
        guardian_confirmations=MemoryTable("guardian_confirmations"),
        csv_files=MemoryTable("csv_files"),
        analyses=MemoryTable("analyses"),
        csv_store=LocalObjectStore(storage_root, csv_bucket),
        results_store=LocalObjectStore(storage_root, results_bucket),
    )
