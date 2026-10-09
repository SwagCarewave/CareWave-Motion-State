from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import Counter
from typing import Callable

from app.config import Settings
from app.services.esp_csi import CsiParseError, parse_line
from app.services.monitoring import SessionManager, SessionNotFound, SessionStopped

log = logging.getLogger("carewave.udp")

SESSION_REFRESH_SEC = 2.0
MAX_BUFFER = 20000


class UdpCollector(asyncio.DatagramProtocol):
    def __init__(self, settings: Settings, sessions: SessionManager, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.sessions = sessions
        self.clock = clock
        self.buffer: list[dict] = []
        self.lock = threading.Lock()
        self.stats: Counter = Counter()
        self.last_ts: dict[str, float] = {}
        self.last_packet_wall: float | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self.listening = False
        self._session_id: str | None = None
        self._session_checked = 0.0

    def connection_made(self, transport) -> None:
        self.transport = transport
        self.listening = True

    def connection_lost(self, exc) -> None:
        self.listening = False

    def datagram_received(self, data: bytes, addr) -> None:
        now = self.clock()
        allowed = self.settings.udp_allowed_sources
        if allowed and (not addr or addr[0] not in allowed):
            self.stats["rejected_source"] += 1
            return
        for line in data.decode("utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            self.stats["received"] += 1
            try:
                packet = parse_line(line)
            except CsiParseError as exc:
                self.stats[f"rejected_{exc.reason}"] += 1
                continue
            ts = now
            last = self.last_ts.get(packet.rx)
            if last is not None and ts <= last:
                ts = last + 1e-6
            self.last_ts[packet.rx] = ts
            self.last_packet_wall = now
            item = {"ts": ts, "rx": packet.rx, "amplitude": packet.amplitude.tolist()}
            with self.lock:
                if len(self.buffer) >= MAX_BUFFER:
                    self.buffer.pop(0)
                    self.stats["dropped_overflow"] += 1
                self.buffer.append(item)

    def status(self) -> dict:
        return {
            "enabled": self.settings.udp_enabled,
            "listening": self.listening,
            "port": self.settings.udp_port,
            "received": self.stats["received"],
            "allowed_sources": list(self.settings.udp_allowed_sources),
            "rejected": sum(v for k, v in self.stats.items() if k.startswith("rejected_")),
            "dropped_no_session": self.stats["dropped_no_session"],
            "last_packet_at": None if self.last_packet_wall is None else round(self.last_packet_wall, 3),
        }

    def flush(self) -> None:
        with self.lock:
            if not self.buffer:
                return
            batch, self.buffer = self.buffer, []
        try:
            for attempt in range(2):
                session_id = self._running_session(force=attempt > 0)
                if session_id is None:
                    self.stats["dropped_no_session"] += len(batch)
                    return
                try:
                    result = self.sessions.ingest(session_id, batch)
                except (SessionStopped, SessionNotFound):
                    continue
                self.stats["accepted"] += result.accepted
                return
            self._session_id = None
            self.stats["dropped_no_session"] += len(batch)
        except Exception:
            log.exception("could not hand %d UDP packets to session %s", len(batch), session_id)
            with self.lock:
                self.buffer = batch + self.buffer
                if len(self.buffer) > MAX_BUFFER:
                    self.stats["dropped_overflow"] += len(self.buffer) - MAX_BUFFER
                    self.buffer = self.buffer[-MAX_BUFFER:]

    def _running_session(self, force: bool = False) -> str | None:
        now = self.clock()
        if not force and self._session_id is not None and now - self._session_checked < SESSION_REFRESH_SEC:
            return self._session_id
        try:
            running = self.sessions.list("running", limit=1)
        except Exception:
            log.exception("could not look up running session")
            return self._session_id
        self._session_id = running[0]["id"] if running else None
        self._session_checked = now
        return self._session_id


async def start_udp(collector: UdpCollector) -> asyncio.DatagramTransport | None:
    if not collector.settings.udp_enabled:
        return None
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: collector, local_addr=(collector.settings.udp_host, collector.settings.udp_port))
    except OSError:
        log.exception("UDP port %s could not be opened; CSI collection over UDP is off",
                      collector.settings.udp_port)
        return None
    log.info("listening for ESP CSI on udp://%s:%s", collector.settings.udp_host, collector.settings.udp_port)
    return transport


async def run_udp_flusher(collector: UdpCollector, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(collector.flush)
        except Exception:
            log.exception("UDP flush failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=collector.settings.udp_flush_sec)
        except asyncio.TimeoutError:
            pass
