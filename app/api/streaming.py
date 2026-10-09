from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import WebSocket, WebSocketDisconnect

from app.services.monitoring import Subscriber


async def drain(websocket: WebSocket) -> None:
    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        return


async def pump(websocket: WebSocket, sub: Subscriber, snapshot: dict, closing: str,
               unsubscribe: Callable[[], None]) -> None:
    receiver = asyncio.create_task(drain(websocket))
    try:
        await websocket.send_json(snapshot)
        while True:
            getter = asyncio.create_task(sub.queue.get())
            done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                getter.cancel()
                break
            message = getter.result()
            await websocket.send_json(message)
            if message.get("type") == closing:
                await websocket.close(code=1000)
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        receiver.cancel()
        unsubscribe()
