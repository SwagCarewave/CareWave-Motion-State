from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.pose.packets import FILE_HEADER, MAGIC, PacketError, iter_file, parse_payload
from app.pose.signals import SignalTracker

NODE_IDS = tuple(f"N{i}" for i in range(1, 7))


class RecordingError(ValueError):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass
class RecordingPreview:
    start_ts: float
    end_ts: float
    packets: int
    records: int
    rejected_packets: int
    by_node: dict[str, int] = field(default_factory=dict)
    links: int = 0

    @property
    def duration_sec(self) -> float:
        return round(self.end_ts - self.start_ts, 3)


def looks_like_recording(head: bytes) -> bool:
    if len(head) < FILE_HEADER.size + 3:
        return False
    stamp, length = FILE_HEADER.unpack(head[:FILE_HEADER.size])
    return 1e9 < stamp < 3e9 and 8 <= length and head[FILE_HEADER.size:FILE_HEADER.size + 3] == MAGIC


def inspect(path: Path) -> RecordingPreview:
    if not looks_like_recording(Path(path).read_bytes()[:FILE_HEADER.size + 3]):
        raise RecordingError("invalid_format", "ESP 6대 녹화 파일(.bin) 형식이 아닙니다. 수집 프로그램이 저장한 원본 파일을 올려 주세요.")
    packets = records = rejected = 0
    start = end = None
    nodes: Counter = Counter()
    links: set[str] = set()
    with Path(path).open("rb") as f:
        for stamp, payload in iter_file(f):
            if payload[:3] != MAGIC:
                rejected += 1
                continue
            try:
                _, recs = parse_payload(payload)
            except PacketError:
                rejected += 1
                continue
            packets += 1
            start = stamp if start is None else min(start, stamp)
            end = stamp if end is None else max(end, stamp)
            for r in recs:
                records += 1
                nodes[f"N{r.owner}"] += 1
                links.add(r.link)
    if not records or start is None or end <= start:
        raise RecordingError("no_valid_rows", "분석할 수 있는 CSI 기록이 없습니다. 다른 파일을 선택해 주세요.",
                             {"packets": packets, "rejected_packets": rejected})
    return RecordingPreview(start, end, packets, records, rejected, {k: nodes[k] for k in NODE_IDS if nodes[k]}, len(links))


def analyze(path: Path, runtime, on_progress: Callable[[float], None] | None = None,
            frame_filter: Callable[[dict], dict] | None = None) -> tuple[list[dict], list[dict]]:
    if hasattr(runtime, "infer_last"):
        runtime.infer_last = 1 << 30
    preview_end = None
    first = None
    frames: list[dict] = []
    graph: list[dict] = []
    tracker = SignalTracker(runtime.links)

    def collect(new: list[dict]) -> None:
        for fr in new:
            tracker.set_activity(fr["ts"], fr.get("activity_score"))
        frames.extend(new)

    with Path(path).open("rb") as f:
        stamps = [s for s, _ in iter_file(f)]
    if stamps:
        first, preview_end = stamps[0], stamps[-1]
    span = max((preview_end or 0) - (first or 0), 1e-9)
    last_report = 0.0
    with Path(path).open("rb") as f:
        for stamp, payload in iter_file(f):
            try:
                _, records = parse_payload(payload)
            except PacketError:
                continue
            collect(runtime.poll(stamp))
            graph.extend(tracker.poll(stamp))
            for r in records:
                runtime.add(stamp, r)
                tracker.add(stamp, r)
            if on_progress is not None:
                done = (stamp - first) / span
                if done - last_report >= 0.02:
                    last_report = done
                    on_progress(min(0.99, done))
    if preview_end is not None:
        collect(runtime.poll(preview_end + 0.1))
        graph.extend(tracker.poll(preview_end + tracker.delay))
    if frame_filter is not None:
        frames = [frame_filter(f) for f in frames]
    return frames, graph
