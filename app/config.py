from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:5173", "http://127.0.0.1:5173")

FINAL_MODEL_SHA256 = "89c3b1fab51e50f9817bfe5c9d07e213cd86fd97b3d95ab1ef330512a7bc9a0e"


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _supabase_url(raw: str | None) -> str | None:
    if not raw:
        return None
    url = raw.strip().rstrip("/")
    for suffix in ("/rest/v1", "/storage/v1", "/auth/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url or None


def _env(name: str, default: str) -> str:
    return os.environ.get(f"CAREWAVE_{name}", default)


@dataclass(frozen=True)
class Settings:
    model_path: Path = ROOT / "models" / "lying_walking_v2.pkl"
    model_sha256: str = FINAL_MODEL_SHA256
    cors_origins: tuple[str, ...] = ("*",)
    storage_backend: str = "memory"
    local_storage_dir: Path = ROOT / "outputs" / "storage"
    max_upload_mb: int = 50
    live_retention_sec: int = 3600
    frame_step_sec: float = 0.5
    rx_weak_rate: float = 3.0
    rx_rate_window_sec: float = 2.0
    rx_lost_sec: float = 5.0
    tick_sec: float = 1.0
    stall_sec: float = 3.0
    frame_flush_sec: float = 2.0
    purge_interval_sec: float = 300.0
    max_packets_per_request: int = 5000
    max_gap_sec: float = 600.0
    future_skew_sec: float = 3600.0
    analysis_workers: int = 1
    analysis_progress_step: float = 0.02
    replay_tick_sec: float = 0.5
    replay_idle_sec: float = 3600.0
    results_cache_size: int = 4
    event_burst_sec: float = 10.0
    udp_enabled: bool = False
    udp_host: str = "0.0.0.0"
    udp_port: int = 5005
    udp_flush_sec: float = 0.25
    supabase_url: str | None = None
    supabase_key: str | None = None
    supabase_db_host: str | None = None
    supabase_db_password: str | None = None
    csv_bucket: str = "csv-uploads"
    results_bucket: str = "analysis-results"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def supabase_ref(self) -> str | None:
        if not self.supabase_url:
            return None
        return self.supabase_url.split("//", 1)[-1].split(".", 1)[0]


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    origins = tuple(o.strip() for o in _env("CORS_ORIGINS", ",".join(DEFAULT_ORIGINS)).split(",") if o.strip())
    return Settings(
        model_path=Path(_env("MODEL_PATH", str(ROOT / "models" / "lying_walking_v2.pkl"))),
        model_sha256=_env("MODEL_SHA256", FINAL_MODEL_SHA256),
        cors_origins=origins or ("*",),
        storage_backend=_env("STORAGE_BACKEND", "memory"),
        local_storage_dir=Path(_env("LOCAL_STORAGE_DIR", str(ROOT / "outputs" / "storage"))),
        max_upload_mb=int(_env("MAX_UPLOAD_MB", "50")),
        live_retention_sec=int(_env("LIVE_RETENTION_SEC", "3600")),
        rx_weak_rate=float(_env("RX_WEAK_RATE", "3.0")),
        rx_rate_window_sec=float(_env("RX_RATE_WINDOW_SEC", "2.0")),
        rx_lost_sec=float(_env("RX_LOST_SEC", "5.0")),
        udp_enabled=_env("UDP_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on"),
        udp_host=_env("UDP_HOST", "0.0.0.0"),
        udp_port=int(_env("UDP_PORT", "5005")),
        supabase_url=_supabase_url(os.environ.get("SUPABASE_URL")),
        supabase_key=os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or None,
        supabase_db_host=os.environ.get("SUPABASE_DB_HOST") or None,
        supabase_db_password=os.environ.get("SUPABASE_DB_PASSWORD") or None,
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings()
