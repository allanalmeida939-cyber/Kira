"""Clima: entende "vai chover amanhã em Fortaleza?", resolve a cidade e monta o painel com os dados exatos.

O Gemini recebe só números e condições normalizados (para narrar); o painel mostra a previsão completa.
Sem cidade dita nem localização autorizada salva, a Kira pergunta a cidade em vez de adivinhar.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from services.controllers.charts import validate_chart
from services.controllers.research import ResearchController, SectionPlan
from services.media_topics import fold
from services.providers.weather import Weather, WeatherProvider

PRESENTATION_ID = "weather-kira"
WEEKDAYS = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")
LOCATION_PATH = Path(__file__).resolve().parents[2] / "configs" / "location.json"


def resolve_day(when: str, *, today: date | None = None) -> int:
    """Índice do dia pedido (0 = hoje). Sem dia reconhecido, hoje."""
    base = today or date.today()
    text = fold(when or "")
    if "depois de amanha" in text:
        return 2
    if "amanha" in text:
        return 1
    for index, name in enumerate(WEEKDAYS):
        if name in text.split() or f"{name}-feira" in text or f"{name} feira" in text:
            return (index - base.weekday()) % 7
    return 0


class LocationStore:
    """Cidade autorizada pelo usuário para "como está o tempo" sem cidade (arquivo local editável)."""

    def __init__(self, path: Path = LOCATION_PATH) -> None:
        self._path = path

    def get(self) -> str | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        city = " ".join(str(data.get("city") or "").split())[:80] if isinstance(data, dict) else ""
        return city if city and data.get("authorized") is True else None

    def set(self, city: str | None) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"city": " ".join(str(city or "").split())[:80], "authorized": bool(city)}
        handle, temp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
        os.replace(temp, self._path)


def narration_facts(weather: Weather, day_index: int) -> dict[str, Any]:
    """Resumo compacto para o Gemini narrar: só números e condições, sem texto livre de fora."""
    days = weather.forecast
    day = days[min(day_index, len(days) - 1)]
    facts: dict[str, Any] = {
        "cidade": ", ".join(p for p in (weather.location.name, weather.location.region) if p),
        "dia": day.date,
        "condicao": day.condition,
        "minima_c": day.min,
        "maxima_c": day.max,
        "chance_de_chuva_pct": day.precipitationProbability,
        "periodos": [{k: p[k] for k in ("period", "temperature", "precipitationProbability", "condition")} for p in day.periods],
        "fonte": weather.source["name"],
    }
    if day_index == 0:
        facts["agora"] = {k: weather.current.get(k) for k in ("temperature", "apparentTemperature", "condition", "humidity")}
    return facts


class WeatherController:
    def __init__(self, research: ResearchController, provider: WeatherProvider | None = None, locations: LocationStore | None = None, *, clock: Callable[[], datetime] = datetime.now) -> None:
        self._research = research
        self._provider = provider or WeatherProvider()
        self._locations = locations or LocationStore()
        self._clock = clock
        self._last_city = ""

    @property
    def locations(self) -> LocationStore:
        return self._locations

    async def ask(self, *, city: str | None, when: str = "", turn_id: str | None = None) -> dict[str, Any]:
        # Sem cidade na frase: a autorizada; senão a última que o usuário disse nesta sessão (só em memória).
        place = " ".join(str(city or "").split()) or self._locations.get() or self._last_city
        if not place:
            return {"ok": False, "code": "sem_cidade", "message": "Pergunte ao usuário de qual cidade ele quer a previsão."}
        day_index = resolve_day(when, today=self._clock().date())
        if day_index > 5:
            return {"ok": False, "code": "fora_do_alcance", "message": "A previsão disponível cobre só os próximos 6 dias."}
        holder: dict[str, Weather] = {}
        fetch = asyncio.ensure_future(self._provider.forecast(place))

        async def load() -> dict[str, Any]:
            weather = await fetch
            holder["weather"] = weather
            self._last_city = place
            data = weather.as_dict()
            data["focusDay"] = day_index
            source = {"id": "open-meteo", "name": weather.source["name"], "url": weather.source["url"], "accessedAt": weather.updatedAt}
            return {"data": data, "sources": [source]}

        async def chart() -> dict[str, Any]:
            weather = await fetch
            labels = [day.date[8:10] + "/" + day.date[5:7] for day in weather.forecast]
            model = validate_chart({
                "chartId": "temperatura", "chartType": "line", "title": f"Temperatura em {weather.location.name}",
                "xAxis": {"label": "dia", "labels": labels}, "yAxis": {"label": "temperatura"},
                "series": [
                    {"name": "máxima", "unit": "°C", "data": [{"value": d.max, "kind": "forecast"} for d in weather.forecast]},
                    {"name": "mínima", "unit": "°C", "data": [{"value": d.min, "kind": "forecast"} for d in weather.forecast]},
                ],
                "sourceIds": ["open-meteo"], "updatedAt": weather.updatedAt,
            })
            return {"data": model}

        result = await self._research.run(
            presentation_id=PRESENTATION_ID, title=f"Previsão do tempo: {place}", turn_id=turn_id,
            sections=[
                SectionPlan("clima", "weather", "Previsão", load),
                SectionPlan("grafico", "chart", "Temperatura nos próximos dias", chart),
                SectionPlan("fontes", "sources", "Fonte", self._no_data),
            ],
        )
        if not fetch.done():
            fetch.cancel()
        weather = holder.get("weather")
        if weather is None:
            reason = result.sections.get("clima", {}).get("reason", "o serviço de previsão não respondeu")
            return {"ok": False, "code": "indisponivel", "message": f"Não consegui obter a previsão atual: {reason}."}
        return {"ok": True, "previsao": narration_facts(weather, day_index)}

    @staticmethod
    async def _no_data() -> dict[str, Any]:
        return {"data": {}}


__all__ = ["LocationStore", "PRESENTATION_ID", "WeatherController", "narration_facts", "resolve_day"]
