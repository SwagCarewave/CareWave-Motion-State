from __future__ import annotations

import json
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from .base import BackendUnavailable, ObjectNotFound, Repositories, split_filter

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RepositoryError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"supabase request failed ({status}): {body[:300]}")
        self.status = status
        self.body = body


class SupabaseHttp:
    def __init__(self, url: str, key: str, attempts: int = 3, backoff: float = 0.5,
                 transport: httpx.BaseTransport | None = None):
        self.attempts = attempts
        self.backoff = backoff
        self.client = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(30.0, connect=5.0),
            transport=transport,
        )

    def request(self, service: str, method: str, path: str, **kwargs) -> httpx.Response:
        last: str = ""
        for attempt in range(self.attempts):
            if attempt:
                time.sleep(self.backoff * (2 ** (attempt - 1)))
            try:
                res = self.client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
            if res.status_code in RETRY_STATUS:
                last = f"HTTP {res.status_code}"
                continue
            return res
        raise BackendUnavailable(service, f"{service} 연결 실패 ({last})")

    def close(self) -> None:
        self.client.close()


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _quoted(value: Any) -> str:
    if isinstance(value, (bool, int, float)) or value is None:
        return _literal(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_params(filters: dict[str, Any] | None) -> list[tuple[str, str]]:
    params = []
    for key, value in (filters or {}).items():
        field, op = split_filter(key)
        if op == "is" or (op == "eq" and value is None):
            params.append((field, f"is.{_literal(value)}"))
        elif op == "in":
            params.append((field, "in.(" + ",".join(_quoted(v) for v in value) + ")"))
        else:
            params.append((field, f"{op}.{_literal(value)}"))
    return params


class SupabaseTable:
    def __init__(self, http: SupabaseHttp, name: str):
        self.http = http
        self.name = name
        self.path = f"/rest/v1/{name}"

    def _check(self, res: httpx.Response) -> Any:
        if res.status_code >= 400:
            raise RepositoryError(res.status_code, res.text)
        return res.json() if res.content else None

    def insert(self, row: dict) -> dict:
        return self.insert_many([row])[0]

    def insert_many(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        body = [{**r, "id": r.get("id") or str(uuid.uuid4())} for r in rows]
        res = self.http.request("database", "POST", self.path, params={"on_conflict": "id"},
                                content=json.dumps(body, ensure_ascii=False),
                                headers={"Content-Type": "application/json",
                                         "Prefer": "return=representation,resolution=ignore-duplicates"})
        out = self._check(res) or []
        if len(out) == len(body):
            return out
        got = {r["id"] for r in out}
        missing = [r["id"] for r in body if r["id"] not in got]
        return out + self.list({"id__in": missing})

    def upsert(self, row: dict, on_conflict: tuple[str, ...]) -> dict:
        res = self.http.request("database", "POST", self.path, params={"on_conflict": ",".join(on_conflict)},
                                content=json.dumps([row], ensure_ascii=False),
                                headers={"Content-Type": "application/json",
                                         "Prefer": "return=representation,resolution=merge-duplicates"})
        return self._check(res)[0]

    def get(self, row_id: str) -> dict | None:
        rows = self.list({"id": row_id}, limit=1)
        return rows[0] if rows else None

    def list(self, filters: dict[str, Any] | None = None, order_by: str | None = None, desc: bool = False,
             limit: int | None = None, offset: int = 0) -> list[dict]:
        params = [("select", "*"), *to_params(filters)]
        if order_by is not None:
            params.append(("order", f"{order_by}.{'desc' if desc else 'asc'}.nullslast"))
        if limit is not None:
            params.append(("limit", str(limit)))
        if offset:
            params.append(("offset", str(offset)))
        return self._check(self.http.request("database", "GET", self.path, params=params)) or []

    def update(self, row_id: str, changes: dict[str, Any]) -> dict | None:
        res = self.http.request("database", "PATCH", self.path, params=to_params({"id": row_id}),
                                content=json.dumps(changes, ensure_ascii=False),
                                headers={"Content-Type": "application/json", "Prefer": "return=representation"})
        rows = self._check(res) or []
        return rows[0] if rows else None

    def delete(self, row_id: str) -> bool:
        return self.delete_where({"id": row_id}) > 0

    def delete_where(self, filters: dict[str, Any]) -> int:
        if not filters:
            raise ValueError("delete_where needs at least one filter")
        res = self.http.request("database", "DELETE", self.path, params=[("select", "id"), *to_params(filters)],
                                headers={"Prefer": "return=representation"})
        return len(self._check(res) or [])


class SupabaseObjectStore:
    def __init__(self, http: SupabaseHttp, bucket: str):
        self.http = http
        self.bucket = bucket

    def _object(self, path: str) -> str:
        return f"/storage/v1/object/{self.bucket}/{quote(path)}"

    @staticmethod
    def _missing(res: httpx.Response) -> bool:
        if res.status_code == 404:
            return True
        if res.status_code == 400:
            try:
                body = res.json()
            except ValueError:
                return False
            return str(body.get("statusCode")) == "404" or "not found" in str(body.get("error", "")).lower()
        return False

    def put(self, path: str, data: bytes, content_type: str) -> None:
        res = self.http.request("storage", "POST", self._object(path), content=data,
                                headers={"Content-Type": content_type, "x-upsert": "true"},
                                timeout=httpx.Timeout(180.0, connect=5.0))
        if res.status_code >= 400:
            raise RepositoryError(res.status_code, res.text)

    def get(self, path: str) -> bytes:
        res = self.http.request("storage", "GET", self._object(path), timeout=httpx.Timeout(180.0, connect=5.0))
        if self._missing(res):
            raise ObjectNotFound(path)
        if res.status_code >= 400:
            raise RepositoryError(res.status_code, res.text)
        return res.content

    def delete(self, paths: list[str]) -> None:
        if not paths:
            return
        res = self.http.request("storage", "DELETE", f"/storage/v1/object/{self.bucket}",
                                json={"prefixes": list(paths)})
        if res.status_code >= 400 and not self._missing(res):
            raise RepositoryError(res.status_code, res.text)

    def exists(self, path: str) -> bool:
        res = self.http.request("storage", "GET", f"/storage/v1/object/info/{self.bucket}/{quote(path)}")
        if res.status_code == 200:
            return True
        if self._missing(res):
            return False
        raise RepositoryError(res.status_code, res.text)


def supabase_repositories(url: str, key: str, csv_bucket: str, results_bucket: str,
                          transport: httpx.BaseTransport | None = None) -> Repositories:
    http = SupabaseHttp(url, key, transport=transport)
    return Repositories(
        backend="supabase",
        sessions=SupabaseTable(http, "sessions"),
        device_status=SupabaseTable(http, "device_status"),
        signal_frames=SupabaseTable(http, "signal_frames"),
        activity_events=SupabaseTable(http, "activity_events"),
        guardian_confirmations=SupabaseTable(http, "guardian_confirmations"),
        csv_files=SupabaseTable(http, "csv_files"),
        analyses=SupabaseTable(http, "analyses"),
        csv_store=SupabaseObjectStore(http, csv_bucket),
        results_store=SupabaseObjectStore(http, results_bucket),
    )
