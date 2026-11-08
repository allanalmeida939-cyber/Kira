"""Briefing do dia: clima, agenda, notícias, cotações, hoje na história e lembretes em paralelo; uma fonte falha nunca
segura as outras.

Configuração em configs/briefing.json (categorias, ordem, privacidade). E-mail só entra com autorização explícita.
A Kira narra seção por seção ("próximo"/"anterior"); interrupção congela a narração.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from services.controllers.news import group_articles, mark_told, not_told
from services.controllers.personal import enabled
from services.controllers.research import ResearchController, SectionPlan
from services.controllers.weather import LocationStore, narration_facts
from services.providers.calendar import TIMEZONE, CalendarProvider, as_dicts as events_as_dicts
from services.providers.email import EmailProvider, as_dicts as emails_as_dicts
from services.providers.news import NewsProvider
from services.providers.weather import WeatherProvider

PRESENTATION_ID = "briefing-kira"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "briefing.json"
CATEGORIES = ("clima", "agenda", "noticias", "cotacoes", "historia", "lembretes", "email")
DEFAULTS = {"categories": ["clima", "agenda", "noticias", "lembretes"], "emailAuthorized": False, "newsTopics": []}
HEADINGS = {"clima": "Clima", "agenda": "Agenda", "noticias": "Notícias", "cotacoes": "Cotações", "historia": "Hoje na história",
            "lembretes": "Lembretes", "email": "E-mails"}
TYPES = {"clima": "weather", "agenda": "agenda", "noticias": "news", "cotacoes": "text", "historia": "text", "lembretes": "text",
         "email": "email"}
# Só entram quando ligadas na aba Kira (desligadas por padrão).
OPT_IN = ("cotacoes", "historia")
CURRENCIES = {"USD": "Dólar", "EUR": "Euro", "BTC": "Bitcoin"}


def brl(value: object) -> str:
    """5.4187 -> 'R$ 5,42'; 612345.1 -> 'R$ 612.345,10'."""
    return "R$ " + f"{float(value):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def percent(value: object) -> str:
    return f"{float(value):+.2f}%".replace(".", ",")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    categories = [c for c in raw.get("categories") or DEFAULTS["categories"] if c in CATEGORIES]
    return {
        "categories": list(dict.fromkeys(categories)) or list(DEFAULTS["categories"]),
        "emailAuthorized": raw.get("emailAuthorized") is True,
        "newsTopics": [str(t)[:30] for t in raw.get("newsTopics") or []][:4],
    }


class BriefingController:
    def __init__(
        self,
        research: ResearchController,
        *,
        weather: WeatherProvider,
        locations: LocationStore,
        news: NewsProvider,
        calendar: CalendarProvider,
        email: EmailProvider,
        reminders: Callable[[], list[Any]],
        widgets: Any = None,
        config_path: Path = CONFIG_PATH,
        clock: Callable[[], datetime] = lambda: datetime.now(TIMEZONE),
    ) -> None:
        self._research = research
        self._weather, self._locations, self._news = weather, locations, news
        self._calendar, self._email, self._reminders = calendar, email, reminders
        self._widgets = widgets  # cotações e "hoje na história" (os mesmos dados dos widgets)
        self._config_path = config_path
        self._clock = clock
        self._state: dict[str, Any] = {}

    def _categories(self, config: dict[str, Any]) -> list[str]:
        """Só o que pode aparecer: seções ligadas na aba Kira e com fonte disponível (sem cidade, sem agenda ligada,
        e-mail sem autorização ficam de fora; a Kira nem fala delas)."""
        from services.kira_settings import load as load_settings

        wanted = load_settings().get("briefing_sections") or {}
        names = []
        for name in config["categories"]:
            if wanted.get(name) is False:
                continue
            if name == "clima" and not self._locations.get():
                continue
            if name == "agenda" and not enabled("KIRA_CALENDAR_ENABLED"):
                continue
            if name == "email" and not (config["emailAuthorized"] and enabled("KIRA_EMAIL_ENABLED")):
                continue
            names.append(name)
        extras = [name for name in OPT_IN if self._widgets is not None and wanted.get(name) is True]
        # as seções extras entram antes dos lembretes; o resto fica na ordem preferida (configs/briefing.json)
        at = names.index("lembretes") if "lembretes" in names else len(names)
        return names[:at] + extras + names[at:]

    def _plans(self, config: dict[str, Any], facts: dict[str, Any], names: list[str]) -> list[SectionPlan]:
        now = self._clock()

        async def clima() -> dict[str, Any]:
            city = self._locations.get()
            if not city:
                raise ValueError("sem cidade autorizada para o clima")
            weather = await self._weather.forecast(city)
            facts["clima"] = narration_facts(weather, 0)
            return {"data": {**weather.as_dict(), "focusDay": 0}}

        async def agenda() -> dict[str, Any]:
            if not enabled("KIRA_CALENDAR_ENABLED"):
                raise ValueError("agenda desligada nas configurações")
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            events = events_as_dicts(await self._calendar.events(now, start + timedelta(days=1)))
            facts["agenda"] = [{"titulo": e["title"], "inicio": e["startAt"][11:16], "conflito": bool(e["conflictsWith"])} for e in events]
            return {"data": {"events": events, "period": "hoje"}}

        async def noticias() -> dict[str, Any]:
            from services.kira_settings import load as load_settings

            settings = load_settings()
            topics = config["newsTopics"] or settings.get("news_topics") or None
            count = settings.get("briefing_news_count") if settings.get("briefing_news_count") in (3, 4, 6) else 4
            articles = not_told(group_articles(await self._news.topic_items(topics)))[:count]
            if not articles:
                # Tudo já foi contado nesta sessão (ex.: "quais as notícias?" antes do briefing): a seção fica de fora.
                raise ValueError("sem notícias novas desde a última vez")
            mark_told(articles)
            facts["noticias"] = [{"titulo": a["title"], "veiculo": a["publisher"], "preliminar": a["preliminary"]} for a in articles]
            return {"data": {"articles": articles}}

        async def lembretes() -> dict[str, Any]:
            upcoming = []
            for item in self._reminders():
                if getattr(item, "status", "") not in {"scheduled", "snoozed"}:
                    continue
                try:
                    due = datetime.fromisoformat(str(item.due_at)).astimezone(TIMEZONE)
                except ValueError:
                    continue
                if due.date() == now.date():
                    upcoming.append((due, str(item.text)[:120]))
            upcoming.sort()
            facts["lembretes"] = [{"hora": d.strftime("%H:%M"), "texto": t} for d, t in upcoming]
            points = [f"{d.strftime('%H:%M')} · {t}" for d, t in upcoming] or ["Nenhum lembrete para hoje."]
            return {"data": {"points": points}}

        async def email() -> dict[str, Any]:
            if not (config["emailAuthorized"] and enabled("KIRA_EMAIL_ENABLED")):
                raise ValueError("e-mail fora do briefing: sem autorização")
            messages = emails_as_dicts(await self._email.recent())[:5]
            facts["email"] = [{"prioridade": m["priority"], "assunto": m["subject"]} for m in messages]
            return {"data": {"messages": messages}}

        async def cotacoes() -> dict[str, Any]:
            rows = (await self._widgets.quotes()).get("items") or []
            quotes = []
            for row in rows:
                code = str(row.get("pair") or "")[:3]
                if code in CURRENCIES:
                    quotes.append({"moeda": CURRENCIES[code], "valor": brl(row["bid"]), "variacao": percent(row.get("pct") or 0)})
            if not quotes:
                raise ValueError("sem cotações agora")
            facts["cotacoes"] = quotes
            return {"data": {"points": [f"{q['moeda']} · {q['valor']} · {q['variacao']}" for q in quotes]}}

        async def historia() -> dict[str, Any]:
            events = [e for e in (await self._widgets.history()).get("items") or [] if e.get("text")][:2]
            if not events:
                raise ValueError("sem fatos de hoje na história")
            facts["historia"] = [{"ano": e.get("year"), "fato": str(e["text"])[:200]} for e in events]
            return {"data": {"points": [f"{e.get('year')} · {str(e['text'])[:160]}" for e in events]}}

        tasks = {"clima": clima, "agenda": agenda, "noticias": noticias, "cotacoes": cotacoes, "historia": historia, "lembretes": lembretes,
                 "email": email}
        return [SectionPlan(name, TYPES[name], HEADINGS[name], tasks[name]) for name in names]

    async def run(self, *, turn_id: str | None = None) -> dict[str, Any]:
        config = load_config(self._config_path)
        names = self._categories(config)
        facts: dict[str, Any] = {}
        result = await self._research.run(
            presentation_id=PRESENTATION_ID, title=f"Briefing de {self._clock().strftime('%d/%m')}", turn_id=turn_id,
            sections=self._plans(config, facts, names), mode="stage",
        )
        order = [name for name in names if result.sections.get(name, {}).get("status") == "ready"]
        self._state = {"request_id": result.request_id, "order": order, "index": -1}
        if not order:
            return {"ok": False, "message": "Não consegui montar o briefing agora."}
        # Só as seções que vão aparecer; o que falhou ou está desligado não é citado.
        return {
            "ok": True, "ordem": order, "secoes": {name: facts.get(name) for name in order},
            "como_falar": "Fale todas as seções numa fala só, nessa ordem, sem parar nem esperar o usuário; diga o nome de cada seção ao começar.",
        }

    async def step(self, direction: str) -> dict[str, Any]:
        state = self._state
        if not state.get("order"):
            return {"ok": False, "message": "Não há briefing aberto."}
        delta = -1 if str(direction).lower().startswith(("ant", "vol", "prev")) else 1
        index = min(max(state["index"] + delta, 0), len(state["order"]) - 1)
        moved = await self._research.narrate(PRESENTATION_ID, state["request_id"], state["order"][index], user_request=True)
        if not moved:
            return {"ok": False, "message": "Esse briefing foi substituído por outro pedido."}
        state["index"] = index
        return {"ok": True, "secao": state["order"][index], "ultima": index == len(state["order"]) - 1}


__all__ = ["BriefingController", "load_config"]
