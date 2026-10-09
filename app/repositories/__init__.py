from __future__ import annotations

from app.config import Settings

from .base import BackendUnavailable, ObjectNotFound, ObjectStore, Repositories, Table
from .memory import LocalObjectStore, MemoryTable, memory_repositories
from .supabase import RepositoryError, supabase_repositories

__all__ = [
    "BackendUnavailable", "ObjectNotFound", "ObjectStore", "Repositories", "Table", "LocalObjectStore",
    "MemoryTable", "RepositoryError", "memory_repositories", "supabase_repositories", "build_repositories",
]


def build_repositories(settings: Settings) -> Repositories:
    if settings.storage_backend == "memory":
        return memory_repositories(settings.local_storage_dir, settings.csv_bucket, settings.results_bucket)
    if settings.storage_backend == "supabase":
        if not settings.supabase_url or not settings.supabase_key:
            raise RuntimeError("CAREWAVE_STORAGE_BACKEND=supabase 에는 SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY 가 필요합니다.")
        return supabase_repositories(settings.supabase_url, settings.supabase_key,
                                     settings.csv_bucket, settings.results_bucket)
    raise ValueError(f"unsupported storage backend: {settings.storage_backend}")
