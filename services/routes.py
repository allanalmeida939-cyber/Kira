"""Rotas e tempo de viagem: de A até B de carro (com caminhos alternativos), moto, bicicleta, a pé e ônibus.

Rotas: OSRM (servidores abertos do OpenStreetMap, sem chave, sem trânsito ao vivo). Ônibus: Transitous (planejador
aberto com os horários publicados; em Fortaleza cobre as linhas metropolitanas da ARCE). Endereços: o buscador do
Olho de Deus. O resultado vira um JSON que a tela desenha num mapa com zoom (interface/assets/js/ui/routeView.js).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger("kira.app.routes")

# perfis do servidor do OpenStreetMap alemão (carro, bicicleta, a pé); o demo do OSRM só tem carro e fica de reserva
OSRM_URLS = {
    "carro": ("https://routing.openstreetmap.de/routed-car/route/v1/driving", "https://router.project-osrm.org/route/v1/driving"),
    "bike": ("https://routing.openstreetmap.de/routed-bike/route/v1/driving",),
    "a_pe": ("https://routing.openstreetmap.de/routed-foot/route/v1/driving",),
}
TRANSIT_URL = "https://api.transitous.org/api/v1/plan"
MODES = ("carro", "moto", "bike", "a_pe", "onibus")
MODE_NAMES = {"carro": "de carro", "moto": "de moto", "bike": "de bicicleta", "a_pe": "a pé", "onibus": "de ônibus"}
MOTO_FACTOR = 0.85  # sem perfil de moto aberto: estimativa pelo carro (passa no corredor, estaciona perto)
MAX_POINTS = 600
MAX_BUS = 5
LOCAL_TZ = timezone(timedelta(hours=-3))  # Fortaleza não tem horário de verão
WEEKDAYS = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
KEEP_PLANS = 30

Fetch = Callable[..., Awaitable[httpx.Response]]
Geocode = Callable[[str], Awaitable[dict[str, Any] | None]]
GeocodeNear = Callable[[str, float, float], Awaitable[dict[str, Any] | None]]
FAR_KM = 300.0  # destino mais longe que isso, sem cidade no pedido: confirma antes (era "35 horas até o hospital")
CITY_HINT = re.compile(r",|\b(em|no|na|de)\s+[A-ZÁÉÍÓÚ][a-zà-ú]+|\b(ceara|ce|sp|rj|são paulo|sao paulo|rio de janeiro|bahia|pernambuco|minas)\b", re.IGNORECASE)


async def _get(url: str, **params: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=25.0, headers={"User-Agent": "Kira/8 (assistente pessoal local)"}) as client:
        response = await client.get(url, params=params or None)
    response.raise_for_status()
    return response


def duration_text(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d} min" if rest else f"{hours} h"


def distance_text(meters: float) -> str:
    return f"{round(meters)} m" if meters < 1000 else f"{meters / 1000:.1f} km".replace(".", ",")


def thin(path: list[list[float]], limit: int = MAX_POINTS) -> list[list[float]]:
    """Menos pontos para a tela (o desenho continua igual a olho), sempre com o primeiro e o último."""
    if len(path) <= limit:
        return path
    step = len(path) / (limit - 1)
    return [path[int(i * step)] for i in range(limit - 1)] + [path[-1]]


def parse_routes(data: dict) -> list[dict[str, Any]]:
    out = []
    for route in (data.get("routes") or []) if data.get("code") == "Ok" else []:
        coords = ((route.get("geometry") or {}).get("coordinates")) or []
        out.append({"seconds": float(route.get("duration") or 0), "meters": float(route.get("distance") or 0),
                    "path": thin([[round(float(lat), 6), round(float(lon), 6)] for lon, lat in coords])})
    return out


def decode_polyline(text: str, precision: int = 7) -> list[list[float]]:
    """Linha codificada (formato do Google, usado pelo Transitous) -> [[lat, lon], ...]."""
    coords: list[list[float]] = []
    index, lat, lon, factor = 0, 0, 0, 10 ** precision
    while index < len(text):
        values = []
        for _ in (0, 1):
            shift = result = 0
            while index < len(text):
                byte = ord(text[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            values.append(~(result >> 1) if result & 1 else result >> 1)
        lat += values[0]
        lon += values[1]
        coords.append([round(lat / factor, 6), round(lon / factor, 6)])
    return coords


def _clock_text(iso: str, now: datetime) -> str:
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
    except (ValueError, AttributeError):
        return ""
    hour = when.strftime("%H:%M")
    return hour if when.date() == now.astimezone(LOCAL_TZ).date() else f"{WEEKDAYS[when.weekday()]} {when.strftime('%d/%m')} {hour}"


def parse_transit(data: dict, now: datetime) -> list[dict[str, Any]]:
    options = []
    for it in (data.get("itineraries") or [])[:MAX_BUS]:
        legs, lines, walk = [], [], 0.0
        for leg in it.get("legs") or []:
            geometry = leg.get("legGeometry") or {}
            path = thin(decode_polyline(str(geometry.get("points") or ""), int(geometry.get("precision") or 7)), 300)
            mode = "a_pe" if leg.get("mode") == "WALK" else "onibus"
            legs.append({"mode": mode, "path": path})
            if mode == "a_pe":
                walk += float(leg.get("distance") or 0)
                continue
            lines.append({"line": str(leg.get("routeShortName") or "")[:12], "name": str(leg.get("routeLongName") or "")[:60],
                          "headsign": str(leg.get("headsign") or "")[:40], "from": str((leg.get("from") or {}).get("name") or "")[:60],
                          "to": str((leg.get("to") or {}).get("name") or "")[:60], "depart": _clock_text(str(leg.get("startTime") or ""), now)})
        if not lines:
            continue
        options.append({"seconds": int(it.get("duration") or 0), "duration": duration_text(float(it.get("duration") or 0)),
                        "transfers": int(it.get("transfers") or 0), "walk": distance_text(walk), "lines": lines, "legs": legs,
                        "leave": _clock_text(str(it.get("startTime") or ""), now)})
    return sorted(options, key=lambda option: option["seconds"])


# nomes que a transcrição de voz costuma errar ("Unimédio" = Unimed)
HEARD_FIXES = {r"\bunim[eé]di[oa]s?\b": "Unimed", r"\bigua?temi\b": "Iguatemi", r"\bnorte\s*shopping\b": "North Shopping"}


def fix_heard(text: str) -> str:
    for wrong, right in HEARD_FIXES.items():
        text = re.sub(wrong, right, text, flags=re.IGNORECASE)
    return text


class RoutePlanner:
    def __init__(self, geocode: Geocode, *, geocode_near: GeocodeNear | None = None, fetch: Fetch = _get,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._geocode = geocode
        self._geocode_near = geocode_near
        self._fetch = fetch
        self._clock = clock
        self._plans: dict[str, dict[str, Any]] = {}

    async def _osrm(self, mode: str, a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
        where = f"{a['lon']:.6f},{a['lat']:.6f};{b['lon']:.6f},{b['lat']:.6f}"
        extra = {"alternatives": "3"} if mode == "carro" else {}
        for base in OSRM_URLS[mode]:
            try:
                routes = parse_routes((await self._fetch(f"{base}/{where}", overview="full", geometries="geojson", **extra)).json())
            except (httpx.HTTPError, ValueError) as exc:
                logger.info("ROUTE_SOURCE_FAILED mode=%s reason=%s", mode, type(exc).__name__)
                continue
            if routes:
                return routes
        return []

    async def _transit(self, a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
        now = self._clock()
        try:
            data = (await self._fetch(TRANSIT_URL, fromPlace=f"{a['lat']:.6f},{a['lon']:.6f}", toPlace=f"{b['lat']:.6f},{b['lon']:.6f}",
                                      time=now.strftime("%Y-%m-%dT%H:%M:%SZ"), numItineraries=MAX_BUS)).json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("ROUTE_TRANSIT_FAILED reason=%s", type(exc).__name__)
            return []
        return parse_transit(data, now)

    async def plan(self, origin: str, destination: str, mode: str = "carro", official: str = "") -> dict[str, Any]:
        mode = mode if mode in MODES else "carro"
        a = await self._geocode(origin)
        if not a:
            return {"ok": False, "message": f"Não achei '{origin}' no mapa. Diga o endereço com bairro e cidade."}
        # destino: primeiro perto de onde ele sai ("o hospital da Unimed" = o da cidade dele), depois no país todo
        # nome oficial (o Gemini traduz "décima região militar" -> "Comando da 10ª Região Militar") e depois o falado
        destination = fix_heard(destination)
        b = None
        for name in dict.fromkeys(n for n in (" ".join(str(official or "").split())[:80], destination) if n):
            b = b or (await self._geocode_near(name, a["lat"], a["lon"]) if self._geocode_near else None)
        if b is None and self._geocode_near and re.search(r"\bregional\b", destination, re.IGNORECASE):
            # "Hospital da Unimed Regional" -> no mapa é "Hospital Regional Unimed"
            reordered = re.sub(r"\s*\bregional\b", "", destination, flags=re.IGNORECASE).replace(" da ", " ").replace(" do ", " ")
            b = await self._geocode_near(re.sub(r"^(hospital)\s+", r" Regional ", reordered, flags=re.IGNORECASE), a["lat"], a["lon"])
        b = b or await self._geocode(destination)
        if not b:
            return {"ok": False, "message": f"Não achei '{destination}' no mapa. Diga o nome com o bairro (e a cidade, se for fora)."}
        far = math.dist((a["lat"], a["lon"]), (b["lat"], b["lon"])) * 111
        if far > FAR_KM and not CITY_HINT.search(destination):
            return {"ok": False, "longe": True, "message": (f"O único '{destination}' que achei fica em {b.get('label') or 'outra cidade'}, a uns "
                                                             f"{round(far)} km. Pergunte ao Chefe o bairro ou a rua desse lugar (ou se é mesmo nessa cidade).")}
        car, bike, foot, bus = await asyncio.gather(self._osrm("carro", a, b), self._osrm("bike", a, b), self._osrm("a_pe", a, b), self._transit(a, b))
        routes = [{"id": f"carro-{i}", "mode": "carro", "alt": i > 0, **r} for i, r in enumerate(car[:3])]
        routes += [{"id": "bike-0", "mode": "bike", "alt": False, **bike[0]}] if bike else []
        routes += [{"id": "a_pe-0", "mode": "a_pe", "alt": False, **foot[0]}] if foot else []
        if not routes and not bus:
            return {"ok": False, "message": "O serviço de rotas não respondeu agora. Tente de novo em instantes."}
        for route in routes:
            route.update(seconds=round(route["seconds"]), duration=duration_text(route["seconds"]), distance=distance_text(route["meters"]))
        best = {route["mode"]: route for route in routes if not route["alt"]}
        modes = []
        for key in MODES:
            if key == "moto" and "carro" in best:
                seconds = best["carro"]["seconds"] * MOTO_FACTOR
                modes.append({"mode": "moto", "seconds": round(seconds), "duration": duration_text(seconds), "distance": best["carro"]["distance"],
                              "estimate": True, "routeId": best["carro"]["id"]})
            elif key == "onibus" and bus:
                modes.append({"mode": "onibus", "seconds": bus[0]["seconds"], "duration": bus[0]["duration"], "distance": "",
                              "estimate": False, "routeId": "", "lines": [line["line"] for line in bus[0]["lines"]]})
            elif key in best:
                modes.append({"mode": key, "seconds": best[key]["seconds"], "duration": best[key]["duration"],
                              "distance": best[key]["distance"], "estimate": False, "routeId": best[key]["id"]})
        if not any(m["mode"] == mode for m in modes):
            mode = modes[0]["mode"]
        main = next(m for m in modes if m["mode"] == mode)
        now = self._clock()
        plan = {
            "ok": True, "id": uuid.uuid4().hex[:12], "mode": mode, "modeName": MODE_NAMES[mode],
            "origin": {"label": a.get("label") or origin, "lat": a["lat"], "lon": a["lon"]},
            "destination": {"label": b.get("label") or destination, "lat": b["lat"], "lon": b["lon"]},
            "duration": main["duration"], "seconds": main["seconds"],
            "arrive": (now + timedelta(seconds=main["seconds"])).astimezone(LOCAL_TZ).strftime("%H:%M"),
            "routes": routes, "modes": modes, "bus": bus,
            "busNote": "" if bus else "Sem linha de ônibus nos dados abertos para esse trajeto.",
        }
        self._plans[plan["id"]] = plan
        while len(self._plans) > KEEP_PLANS:
            self._plans.pop(next(iter(self._plans)))
        return plan

    def get(self, plan_id: str) -> dict[str, Any] | None:
        return self._plans.get(str(plan_id or ""))


__all__ = ["RoutePlanner", "MODES", "MODE_NAMES", "decode_polyline", "distance_text", "duration_text", "parse_routes", "parse_transit", "thin"]
