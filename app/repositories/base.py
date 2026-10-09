from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

OPERATORS = ("eq", "gt", "gte", "lt", "lte", "in", "is")

log = logging.getLogger("carewave.repositories")


class BackendUnavailable(RuntimeError):
    def __init__(self, service: str, message: str):
        super().__init__(message)
        self.service = service
        self.message = message


class RepositoryError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"repository request failed ({status}): {body[:300]}")
        self.status = status
        self.body = body


class ObjectNotFound(KeyError):
    pass


def split_filter(key: str) -> tuple[str, str]:
    field, _, op = key.partition("__")
    op = op or "eq"
    if op not in OPERATORS:
        raise ValueError(f"unsupported filter operator: {op}")
    return field, op


class Table(Protocol):
    name: str

    def insert(self, row: dict) -> dict: ...

    def insert_many(self, rows: list[dict]) -> list[dict]: ...

    def upsert(self, row: dict, on_conflict: tuple[str, ...]) -> dict: ...

    def get(self, row_id: str) -> dict | None: ...

    def list(self, filters: dict[str, Any] | None = None, order_by: str | None = None, desc: bool = False,
             limit: int | None = None, offset: int = 0) -> list[dict]: ...

    def update(self, row_id: str, changes: dict[str, Any]) -> dict | None: ...

    def delete(self, row_id: str) -> bool: ...

    def delete_where(self, filters: dict[str, Any]) -> int: ...


class ObjectStore(Protocol):
    bucket: str

    def put(self, path: str, data: bytes, content_type: str) -> None: ...

    def get(self, path: str) -> bytes: ...

    def delete(self, paths: list[str]) -> None: ...

    def exists(self, path: str) -> bool: ...


@dataclass(frozen=True)
class Repositories:
    backend: str
    sessions: Table
    device_status: Table
    signal_frames: Table
    activity_events: Table
    guardian_confirmations: Table
    csv_files: Table
    analyses: Table
    csv_store: ObjectStore
    results_store: ObjectStore

    def ping(self) -> dict:
        checks = {}
        for name, probe in (("database", lambda: self.csv_files.list(limit=1)),
                            ("storage", lambda: self.csv_store.exists("__ping__"))):
            try:
                probe()
                checks[name] = "ok"
            except BackendUnavailable:
                log.exception("%s health check failed: unreachable", name)
                checks[name] = "unavailable"
            except Exception:
                log.exception("%s health check failed: error response", name)
                checks[name] = "error"
        return checks
