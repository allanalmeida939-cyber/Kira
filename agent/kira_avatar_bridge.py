from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import websockets
    from websockets.server import WebSocketServerProtocol
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False
    logger.warning("[Avatar] websockets nao instalado. Rode: pip install websockets")


class AvatarBridge:
    """WebSocket server que envia eventos para o avatar HTML da Kira."""

    def __init__(self, host: str = "localhost", port: int = 8765):
        self.host = host
        self.port = port
        self._clients: set = set()
        self._server = None
        self._task: Optional[asyncio.Task] = None
        self._shutdown_lock = asyncio.Lock()
        self._running = False

    async def start(self) -> None:
        if not WS_AVAILABLE:
            logger.warning("[Avatar] Bridge nao iniciada - websockets ausente.")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_server())
        logger.info("[Avatar] Bridge iniciada em ws://%s:%d", self.host, self.port)

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if not self._running and (self._task is None or self._task.done()):
                return
            logger.info("AVATAR_BRIDGE_SHUTDOWN_START")
            self._running = False
            if self._server is not None:
                try:
                    self._server.close()
                    await self._server.wait_closed()
                except Exception as exc:
                    logger.warning("[Avatar] Falha ao fechar servidor websocket: %s", exc)
                finally:
                    self._server = None
            for ws in list(self._clients):
                try:
                    await ws.close()
                except Exception:
                    pass
            self._clients.clear()
            if self._task and not self._task.done():
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
            logger.info("AVATAR_BRIDGE_SHUTDOWN_DONE")

    async def speak(self, text: str) -> None:
        await self._broadcast({"type": "speak", "text": text})

    async def speaking_start(self) -> None:
        await self._broadcast({"type": "speaking_start"})

    async def speaking_stop(self) -> None:
        await self._broadcast({"type": "speaking_stop"})

    async def toggle(self) -> None:
        await self._broadcast({"type": "toggle"})

    async def _broadcast(self, payload: dict) -> None:
        if not self._clients:
            return
        msg = json.dumps(payload, ensure_ascii=False)
        dead = set()
        for ws in self._clients:
            try:
                await ws.send(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def _handler(self, ws: "WebSocketServerProtocol") -> None:
        self._clients.add(ws)
        logger.info("[Avatar] Cliente conectado. Total: %d", len(self._clients))
        try:
            async for _ in ws:
                pass
        except Exception:
            pass
        finally:
            self._clients.discard(ws)
            logger.info("[Avatar] Cliente desconectado. Total: %d", len(self._clients))

    async def _run_server(self) -> None:
        try:
            self._server = await websockets.serve(self._handler, self.host, self.port)
            await asyncio.Future()
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False
