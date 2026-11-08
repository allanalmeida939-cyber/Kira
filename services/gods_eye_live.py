"""Olho de Deus ao vivo: raios (Blitzortung) e navios (aisstream.io) chegam por websocket, ponto a ponto.

As conexões só ficam abertas enquanto o globo pede (as camadas ligadas perguntam a cada poucos segundos); sem pedido
por IDLE_STOP_S, fecham sozinhas. Navios precisam da chave grátis do aisstream.io (AISSTREAM_KEY no .env).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Any

logger = logging.getLogger("kira.app.gods_eye")

BLITZ_URLS = ("wss://ws1.blitzortung.org/", "wss://ws7.blitzortung.org/", "wss://ws8.blitzortung.org/")
AIS_URL = "wss://stream.aisstream.io/v0/stream"
AIS_KEY_ENV = "AISSTREAM_KEY"
STRIKE_WINDOW_S = 10 * 60   # raios dos últimos 10 minutos
MAX_STRIKES = 4000
SHIP_TTL_S = 20 * 60        # navio sem posição nova há 20 min sai do mapa
MAX_SHIPS = 1500
IDLE_STOP_S = 120.0
RETRY_S = 15.0


def blitz_decode(data: str) -> str:
    """As mensagens do Blitzortung vêm comprimidas em LZW (um caractere por código)."""
    if not data:
        return ""
    table: dict[int, str] = {}
    current = data[0]
    old = current
    out = [current]
    code = 256
    for char in data[1:]:
        number = ord(char)
        entry = char if number < 256 else table.get(number, old + current)
        out.append(entry)
        current = entry[0]
        table[code] = old + current
        code += 1
        old = entry
    return "".join(out)


def parse_strike(raw: str) -> dict[str, float] | None:
    try:
        data = json.loads(blitz_decode(raw))
        lat, lon = float(data["lat"]), float(data["lon"])
    except (ValueError, KeyError, TypeError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    when = data.get("time")
    seconds = float(when) / 1e9 if isinstance(when, (int, float)) and when > 1e15 else time.time()
    return {"lat": round(lat, 4), "lon": round(lon, 4), "t": seconds}


def parse_ais(raw: str | bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    meta = data.get("MetaData") or {}
    kind = data.get("MessageType")
    body = (data.get("Message") or {}).get(kind) or {}
    try:
        lat, lon, mmsi = float(meta["latitude"]), float(meta["longitude"]), str(meta["MMSI"])
    except (KeyError, TypeError, ValueError):
        return None
    ship: dict[str, Any] = {"mmsi": mmsi, "lat": round(lat, 5), "lon": round(lon, 5), "name": str(meta.get("ShipName") or "").strip()[:30]}
    if kind == "PositionReport":
        heading = body.get("TrueHeading")
        ship["speed_kt"] = round(float(body.get("Sog") or 0), 1)
        ship["course"] = int(heading if isinstance(heading, (int, float)) and heading < 360 else float(body.get("Cog") or 0))
    elif kind == "ShipStaticData":
        ship["dest"] = str(body.get("Destination") or "").strip()[:30]
    return ship


class _Stream:
    """Um websocket que liga quando pedem e desliga parado."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._task: asyncio.Task | None = None
        self._asked = 0.0
        self.error = ""

    def touch(self) -> None:
        self._asked = time.monotonic()
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())

    def idle(self) -> bool:
        return time.monotonic() - self._asked > IDLE_STOP_S

    async def _loop(self) -> None:
        import websockets

        while not self.idle():
            try:
                await self._session(websockets)
                self.error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - rede de fora: qualquer queda tenta de novo
                self.error = type(exc).__name__
                logger.info("GODS_EYE_STREAM_DROPPED stream=%s reason=%s", self.name, self.error)
            if not self.idle():
                await asyncio.sleep(RETRY_S)
        logger.info("GODS_EYE_STREAM_IDLE_STOP stream=%s", self.name)

    async def _session(self, websockets: Any) -> None:  # pragma: no cover - sobrescrito
        raise NotImplementedError


class Lightning(_Stream):
    def __init__(self) -> None:
        super().__init__("raios")
        self.strikes: deque[dict[str, float]] = deque(maxlen=MAX_STRIKES)
        self._server = 0

    async def _session(self, websockets: Any) -> None:
        url = BLITZ_URLS[self._server % len(BLITZ_URLS)]
        self._server += 1
        async with websockets.connect(url, open_timeout=10, ping_interval=30, max_size=2**20) as ws:
            await ws.send('{"a":111}')
            logger.info("GODS_EYE_STREAM_OPEN stream=raios")
            while not self.idle():
                strike = parse_strike(await asyncio.wait_for(ws.recv(), 60))
                if strike:
                    self.strikes.append(strike)

    def recent(self) -> list[dict[str, float]]:
        self.touch()
        limit = time.time() - STRIKE_WINDOW_S
        while self.strikes and self.strikes[0]["t"] < limit:
            self.strikes.popleft()
        return list(self.strikes)


class Ships(_Stream):
    def __init__(self) -> None:
        super().__init__("navios")
        self.ships: dict[str, dict[str, Any]] = {}
        self._box: list[list[float]] | None = None
        self._resubscribe = asyncio.Event()

    @staticmethod
    def key() -> str:
        return str(os.getenv(AIS_KEY_ENV) or "").strip()

    def _subscription(self) -> str:
        return json.dumps({"APIKey": self.key(), "BoundingBoxes": [self._box], "FilterMessageTypes": ["PositionReport", "ShipStaticData"]})

    async def _session(self, websockets: Any) -> None:
        async with websockets.connect(AIS_URL, open_timeout=10, ping_interval=30, max_size=2**20) as ws:
            await ws.send(self._subscription())
            self._resubscribe.clear()
            logger.info("GODS_EYE_STREAM_OPEN stream=navios")
            while not self.idle():
                if self._resubscribe.is_set():
                    self._resubscribe.clear()
                    await ws.send(self._subscription())
                try:
                    raw = await asyncio.wait_for(ws.recv(), 5)
                except asyncio.TimeoutError:
                    continue
                ship = parse_ais(raw)
                if ship:
                    merged = {**self.ships.get(ship["mmsi"], {}), **{k: v for k, v in ship.items() if v != ""}, "t": time.time()}
                    self.ships[ship["mmsi"]] = merged
                    if len(self.ships) > MAX_SHIPS:
                        self.ships.pop(next(iter(self.ships)))

    def near(self, lat: float, lon: float, radius_km: float) -> list[dict[str, Any]] | None:
        """Navios na área; None quando falta a chave."""
        if not self.key():
            return None
        span_lat = max(0.5, min(8.0, radius_km / 111.0))
        span_lon = max(0.5, min(12.0, span_lat * 1.5))
        box = [[round(lat - span_lat, 2), round(lon - span_lon, 2)], [round(lat + span_lat, 2), round(lon + span_lon, 2)]]
        if box != self._box:
            self._box = box
            self._resubscribe.set()
        self.touch()
        limit = time.time() - SHIP_TTL_S
        for mmsi in [m for m, s in self.ships.items() if s["t"] < limit]:
            self.ships.pop(mmsi, None)
        (south, west), (north, east) = box
        return [s for s in self.ships.values() if south <= s["lat"] <= north and west <= s["lon"] <= east]


__all__ = ["Lightning", "Ships", "blitz_decode", "parse_ais", "parse_strike", "AIS_KEY_ENV"]
