from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd

from motion_state.csi_io import RX_IDS, SUB_COLS, read_raw_csi

REQUIRED_COLUMNS = ("timestamp", "rx", *SUB_COLS)
EPOCH = pd.Timestamp(0, tz="UTC")


class CsvValidationError(ValueError):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class CsvPreview:
    rows: int
    packets: int
    incomplete_rows: int
    receivers: list[str]
    packets_by_rx: dict[str, int]
    start_ts: float
    end_ts: float

    @property
    def duration_sec(self) -> float:
        return round(self.end_ts - self.start_ts, 3)

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "packets": self.packets,
            "incomplete_rows": self.incomplete_rows,
            "receivers": self.receivers,
            "packets_by_rx": self.packets_by_rx,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_sec": self.duration_sec,
        }


@dataclass(frozen=True)
class CsiTable:
    origin_ts: float
    offset: np.ndarray
    rx: np.ndarray
    amplitude: np.ndarray
    preview: CsvPreview

    def packets(self) -> Iterator[tuple[float, str, np.ndarray]]:
        for t, r, a in zip(self.offset, self.rx, self.amplitude):
            yield float(t), str(r), a


def _header_missing(path: Path) -> list[str]:
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as handle:
            header = [h.strip() for h in next(csv.reader(handle), [])]
    except (OSError, UnicodeDecodeError, csv.Error):
        return []
    return [c for c in REQUIRED_COLUMNS if c not in header]


def load_csi_csv(path: Path) -> CsiTable:
    try:
        raw = read_raw_csi(Path(path))
    except (UnicodeDecodeError, StopIteration, ValueError, csv.Error, pd.errors.EmptyDataError,
            pd.errors.ParserError) as exc:
        raise CsvValidationError("invalid_format", "CSV 파일 형식이 올바르지 않습니다.", {"reason": str(exc)}) from exc
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise CsvValidationError("missing_columns", "필수 열이 없습니다. 타임스탬프, 수신기, CSI 진폭 항목을 확인하세요.",
                                 {"missing": missing})
    rows = len(raw)
    ts = pd.to_datetime(raw["timestamp"], utc=True, errors="coerce", format="mixed")
    rx = raw["rx"].astype(str).str.strip().str.upper()
    amp = raw[SUB_COLS].apply(pd.to_numeric, errors="coerce")
    located = ts.notna() & rx.isin(RX_IDS)
    complete = amp.notna().all(axis=1)
    keep = located & complete
    if not keep.any():
        absent = _header_missing(Path(path))
        if absent:
            raise CsvValidationError("missing_columns", "필수 열이 없습니다. 타임스탬프, 수신기, CSI 진폭 항목을 확인하세요.",
                                     {"missing": absent})
        raise CsvValidationError("no_valid_rows", "분석할 수 있는 CSI 행이 없습니다. 타임스탬프와 수신기 값을 확인하세요.",
                                 {"rows": rows})
    order = np.argsort(ts[keep].to_numpy(), kind="stable")
    stamps = ts[keep].iloc[order]
    origin = float((stamps.iloc[0] - EPOCH).total_seconds())
    offset = (stamps - stamps.iloc[0]).dt.total_seconds().to_numpy(np.float64)
    rx_arr = rx[keep].to_numpy()[order]
    amp_arr = amp[keep].to_numpy(np.float64)[order]
    counts = {r: int((rx_arr == r).sum()) for r in RX_IDS if (rx_arr == r).any()}
    preview = CsvPreview(
        rows=rows,
        packets=len(offset),
        incomplete_rows=int((located & ~complete).sum()),
        receivers=list(counts),
        packets_by_rx=counts,
        start_ts=origin,
        end_ts=round(origin + float(offset[-1]), 6),
    )
    return CsiTable(origin, offset, rx_arr, amp_arr, preview)
