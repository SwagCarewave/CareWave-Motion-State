from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLE_CSV = ROOT / "data" / "raw_csi" / "test" / "yena_test_02_csi_raw.csv"


@pytest.fixture
def sample_csv() -> Path:
    return SAMPLE_CSV


@pytest.fixture
def settings(tmp_path):
    from app.config import Settings

    return Settings(storage_backend="memory", local_storage_dir=tmp_path / "storage")


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings)) as c:
        yield c
