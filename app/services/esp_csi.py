from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from motion_state.csi_io import RX_IDS

ESP_SUBCARRIERS = 64
VALID_INDEX = tuple(range(1, 27)) + tuple(range(38, 64))
_ARRAY = re.compile(r"\[([^\]]*)\]")


@dataclass(frozen=True)
class CsiPacket:
    rx: str
    rssi: int | None
    amplitude: np.ndarray


class CsiParseError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def parse_line(line: str) -> CsiPacket:
    text = line.strip()
    match = _ARRAY.search(text)
    if not match:
        raise CsiParseError("no_array")
    try:
        nums = [int(float(v)) for v in match.group(1).replace(",", " ").split()]
    except ValueError as exc:
        raise CsiParseError("bad_number") from exc
    if len(nums) < ESP_SUBCARRIERS * 2:
        raise CsiParseError("short_array")
    pairs = np.asarray(nums[:ESP_SUBCARRIERS * 2], dtype=np.float64).reshape(ESP_SUBCARRIERS, 2)
    amplitude = np.hypot(pairs[:, 0], pairs[:, 1])[list(VALID_INDEX)]
    head = text[:match.start()].split(",")
    rx = head[1].strip().upper() if len(head) > 1 else ""
    if rx not in RX_IDS:
        raise CsiParseError("unknown_rx")
    try:
        rssi = int(head[3])
    except (IndexError, ValueError):
        rssi = None
    return CsiPacket(rx=rx, rssi=rssi, amplitude=amplitude)

