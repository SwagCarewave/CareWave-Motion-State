from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

import numpy as np

MAGIC = b"\xb7\xc5\x03"
CSI_TYPE = 1
STATUS_TYPE = 2
HEADER_LEN = 8
RECORD_LEN = 352
STATUS_LEN = 112
SIGNAL_START = 96
SIGNAL_LEN = 256
N_BINS = 128
MASKED_BINS = 2
FILE_HEADER = struct.Struct("<dH")


class PacketError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class LinkRecord:
    owner: int
    peer: int
    entry: bytes

    @property
    def link(self) -> str:
        return f"n{self.owner}_p{self.peer}"

    @property
    def amplitude(self) -> np.ndarray:
        return amplitude(self.entry[SIGNAL_START:SIGNAL_START + SIGNAL_LEN])


def is_mesh_payload(data: bytes) -> bool:
    return data[:3] == MAGIC


def amplitude(signal: bytes) -> np.ndarray:
    pairs = np.frombuffer(signal, dtype=np.int8).astype(np.float32).reshape(N_BINS, 2)
    amp = np.linalg.norm(pairs, axis=1)
    amp[:MASKED_BINS] = 0
    return amp


def parse_payload(payload: bytes, max_node: int = 6) -> tuple[int, list[LinkRecord]]:
    if len(payload) < HEADER_LEN or payload[:3] != MAGIC:
        raise PacketError("bad_magic")
    kind, count = payload[3], payload[5]
    if kind == STATUS_TYPE:
        if len(payload) != HEADER_LEN + count * STATUS_LEN:
            raise PacketError("bad_status_length")
        return kind, []
    if kind != CSI_TYPE:
        raise PacketError("unknown_type")
    if not 1 <= count or len(payload) != HEADER_LEN + count * RECORD_LEN:
        raise PacketError("bad_csi_length")
    owner = payload[4]
    records = []
    for j in range(count):
        rec = payload[HEADER_LEN + j * RECORD_LEN:HEADER_LEN + (j + 1) * RECORD_LEN]
        if struct.unpack_from("<H", rec, 38)[0] != SIGNAL_LEN:
            raise PacketError("bad_signal_length")
        peer = rec[24]
        if not (1 <= owner <= max_node and 1 <= peer <= max_node and owner != peer):
            raise PacketError("bad_node")
        records.append(LinkRecord(owner, peer, bytes(rec)))
    return kind, records


def iter_file(source: BinaryIO) -> Iterator[tuple[float, bytes]]:
    while True:
        head = source.read(FILE_HEADER.size)
        if len(head) < FILE_HEADER.size:
            return
        stamp, length = FILE_HEADER.unpack(head)
        payload = source.read(length)
        if len(payload) < length:
            return
        yield stamp, payload


def iter_records(path: Path) -> Iterator[tuple[float, LinkRecord]]:
    with Path(path).open("rb") as f:
        for stamp, payload in iter_file(f):
            kind, records = parse_payload(payload)
            for r in records:
                yield stamp, r
