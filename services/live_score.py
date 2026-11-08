"""Placar esportivo: football-data.org / API-Football (com chave) ou TheSportsDB como fallback."""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

import httpx

from services.image_proxy import proxied_url
from services.shared_http import ensure_tls, tls_context
from services.media_search import PUBLISHER, _safe_query, _slug, _text

LOCAL_TZ = timezone(timedelta(hours=-3))  # America/Fortaleza, sem horario de verao
TIMEOUT = httpx.Timeout(6.0)
TOTAL_TIMEOUT_SECONDS = 6.0
LIVE_REFRESH_SECONDS = 30.0
LIVE_WATCH_MAX_SECONDS = 3 * 3600
MAX_TODAY_ITEMS = 12
TSDB_BASE = "https://www.thesportsdb.com/api/v1/json/123"
FD_BASE = "https://api.football-data.org/v4"
AF_BASE = "https://v3.football.api-sports.io"
STALE_NOTE = "Fonte gratuita: o placar pode estar desatualizado."
UNAVAILABLE_TEXT = "Não tenho placar ao vivo confiável agora."

TEAM_ALIASES = {
    "fortaleza": "Fortaleza", "leao do pici": "Fortaleza", "ceara": "Ceara", "vozao": "Ceara",
    "flamengo": "Flamengo", "mengao": "Flamengo", "fla": "Flamengo", "palmeiras": "Palmeiras", "verdao": "Palmeiras",
    "corinthians": "Corinthians", "timao": "Corinthians", "sao paulo": "Sao Paulo", "spfc": "Sao Paulo",
    "santos": "Santos", "peixe": "Santos", "gremio": "Gremio", "internacional": "Internacional", "inter": "Internacional",
    "atletico mineiro": "Atletico Mineiro", "atletico mg": "Atletico Mineiro", "galo": "Atletico Mineiro",
    "cruzeiro": "Cruzeiro", "vasco": "Vasco da Gama", "vasco da gama": "Vasco da Gama", "botafogo": "Botafogo",
    "fluminense": "Fluminense", "flu": "Fluminense", "bahia": "Bahia", "vitoria": "Vitoria", "sport": "Sport Recife",
    "sport recife": "Sport Recife", "juventude": "Juventude", "bragantino": "Bragantino", "athletico paranaense": "Athletico Paranaense",
    "athletico": "Athletico Paranaense", "coritiba": "Coritiba", "goias": "Goias", "cuiaba": "Cuiaba",
    "mirassol": "Mirassol", "remo": "Remo", "paysandu": "Paysandu", "santa cruz": "Santa Cruz", "nautico": "Nautico",
    "criciuma": "Criciuma", "chapecoense": "Chapecoense", "avai": "Avai", "ponte preta": "Ponte Preta",
}
LIVE_STATES = frozenset({"live", "paused"})


def score_panel_id(team: str) -> str:
    return f"scoreboard-{_slug(team)}"


TODAY_PANEL_ID = "scoreboard-hoje"


def _fold(value: object) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower().strip()


def canonical_team(raw: str) -> str:
    folded = re.sub(r"\b(futebol clube|esporte clube|clube de regatas|fc|ec|sc|do|time do|o|a)\b", " ", _fold(raw))
    folded = " ".join(folded.split())
    return TEAM_ALIASES.get(folded) or TEAM_ALIASES.get(_fold(raw)) or " ".join(w.capitalize() for w in folded.split()) or _safe_query(raw, 60)


def _score(value: object) -> str:
    text = str(value if value is not None else "").strip()
    return text if re.fullmatch(r"\d{1,3}", text) else "-"


def _parse_dt(value: object) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(LOCAL_TZ)


def _match(**fields: object) -> dict[str, object]:
    base = {
        "league": "", "home": "", "away": "", "homeLogoUrl": "", "awayLogoUrl": "", "homeScore": "-", "awayScore": "-",
        "state": "scheduled", "minute": "", "date": "", "time": "", "venue": "",
    }
    base.update(fields)
    for key in ("homeLogoUrl", "awayLogoUrl"):
        base[key] = proxied_url(base[key])
    return {k: v for k, v in base.items() if v or not k.endswith("LogoUrl")}


class ScoreError(Exception):
    """Fonte esportiva indisponivel (rede, cota ou resposta invalida)."""


class LiveScoreService:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        today: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self._now = now or (lambda: datetime.now(LOCAL_TZ))
        self._today_fn = today
        self._environment = dict(environment) if environment is not None else None
        self._watchers: dict[str, asyncio.Task[object]] = {}
        self._fd_teams: tuple[float, list[dict[str, object]]] | None = None

    def _env(self, key: str) -> str:
        source = self._environment if self._environment is not None else os.environ
        return str(source.get(key) or "").strip()

    def _today(self) -> str:
        return self._today_fn() if self._today_fn else self._now().strftime("%Y-%m-%d")

    @property
    def provider(self) -> str:
        if self._env("FOOTBALL_DATA_API_KEY"):
            return "football-data.org"
        if self._env("API_FOOTBALL_KEY"):
            return "API-Football"
        return "TheSportsDB"

    @property
    def stale(self) -> bool:
        return self.provider == "TheSportsDB"

    def _client(self, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
        merged = {"User-Agent": "KiraAssistant/8.0 python-httpx", "Accept": "application/json", **(headers or {})}
        return httpx.AsyncClient(timeout=TIMEOUT, headers=merged, transport=self._transport, verify=tls_context())

    async def _get(self, client: httpx.AsyncClient, url: str, **params: object) -> dict[str, object]:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ScoreError(type(exc).__name__) from exc
        return payload if isinstance(payload, dict) else {}

    # ---------- football-data.org ----------
    @staticmethod
    def _fd_match(item: dict[str, object]) -> dict[str, object]:
        status = str(item.get("status") or "")
        state = {"IN_PLAY": "live", "PAUSED": "paused", "FINISHED": "finished", "AWARDED": "finished"}.get(status, "scheduled")
        score = (item.get("score") or {}).get("fullTime") or {}
        when = _parse_dt(item.get("utcDate"))
        home, away = item.get("homeTeam") or {}, item.get("awayTeam") or {}
        return _match(
            league=_text((item.get("competition") or {}).get("name"), 80),
            home=_text(home.get("shortName") or home.get("name"), 60), away=_text(away.get("shortName") or away.get("name"), 60),
            homeLogoUrl=str(home.get("crest") or ""), awayLogoUrl=str(away.get("crest") or ""),
            homeScore=_score(score.get("home")), awayScore=_score(score.get("away")), state=state,
            minute={"live": "Ao vivo", "paused": "Intervalo"}.get(state, ""),
            date=when.strftime("%Y-%m-%d") if when else "", time=when.strftime("%H:%M") if when else "",
        )

    async def _fd_team_id(self, client: httpx.AsyncClient, name: str) -> tuple[int, str] | None:
        cached = self._fd_teams
        if cached is None:
            teams = (await self._get(client, f"{FD_BASE}/competitions/BSA/teams")).get("teams")
            cached = self._fd_teams = (0.0, [t for t in teams if isinstance(t, dict)] if isinstance(teams, list) else [])
        target = _fold(name)
        for team in cached[1]:
            names = [_fold(team.get(k)) for k in ("name", "shortName", "tla")]
            if any(target == n or (len(target) > 3 and target in n) for n in names):
                return int(team.get("id") or 0), _text(team.get("shortName") or team.get("name"), 60)
        return None

    async def _fd_lookup(self, name: str) -> dict[str, object]:
        async with self._client({"X-Auth-Token": self._env("FOOTBALL_DATA_API_KEY")}) as client:
            found = await self._fd_team_id(client, name)
            if not found:
                return {"found": False}
            today = self._now()
            matches = (await self._get(
                client, f"{FD_BASE}/teams/{found[0]}/matches",
                dateFrom=(today - timedelta(days=10)).strftime("%Y-%m-%d"), dateTo=(today + timedelta(days=14)).strftime("%Y-%m-%d"),
            )).get("matches")
        return {"found": True, "team": found[1], "matches": [self._fd_match(m) for m in matches or [] if isinstance(m, dict)]}

    async def _fd_today(self) -> list[dict[str, object]]:
        day = self._today()
        async with self._client({"X-Auth-Token": self._env("FOOTBALL_DATA_API_KEY")}) as client:
            matches = (await self._get(client, f"{FD_BASE}/matches", dateFrom=day, dateTo=day)).get("matches")
        return [self._fd_match(m) for m in matches or [] if isinstance(m, dict)]

    # ---------- API-Football ----------
    @staticmethod
    def _af_match(item: dict[str, object]) -> dict[str, object]:
        fixture, teams, goals = item.get("fixture") or {}, item.get("teams") or {}, item.get("goals") or {}
        status = fixture.get("status") or {}
        short = str(status.get("short") or "")
        state = "live" if short in {"1H", "2H", "ET", "BT", "P", "LIVE"} else "paused" if short == "HT" else "finished" if short in {"FT", "AET", "PEN"} else "scheduled"
        when = _parse_dt(fixture.get("date"))
        elapsed = status.get("elapsed")
        return _match(
            league=_text((item.get("league") or {}).get("name"), 80),
            home=_text((teams.get("home") or {}).get("name"), 60), away=_text((teams.get("away") or {}).get("name"), 60),
            homeLogoUrl=str((teams.get("home") or {}).get("logo") or ""), awayLogoUrl=str((teams.get("away") or {}).get("logo") or ""),
            homeScore=_score(goals.get("home")), awayScore=_score(goals.get("away")), state=state,
            minute=f"{elapsed}'" if state == "live" and elapsed else "Intervalo" if state == "paused" else "",
            date=when.strftime("%Y-%m-%d") if when else "", time=when.strftime("%H:%M") if when else "",
            venue=_text((fixture.get("venue") or {}).get("name"), 80),
        )

    async def _af_lookup(self, name: str) -> dict[str, object]:
        async with self._client({"x-apisports-key": self._env("API_FOOTBALL_KEY")}) as client:
            teams = (await self._get(client, f"{AF_BASE}/teams", search=name, country="Brazil")).get("response")
            if not isinstance(teams, list) or not teams:
                return {"found": False}
            team = (teams[0] or {}).get("team") or {}
            team_id = team.get("id")
            last, nxt, live = await asyncio.gather(
                self._get(client, f"{AF_BASE}/fixtures", team=team_id, last=1, timezone="America/Fortaleza"),
                self._get(client, f"{AF_BASE}/fixtures", team=team_id, next=1, timezone="America/Fortaleza"),
                self._get(client, f"{AF_BASE}/fixtures", team=team_id, live="all", timezone="America/Fortaleza"),
            )
        items = [i for p in (last, nxt, live) for i in (p.get("response") or []) if isinstance(i, dict)]
        return {"found": True, "team": _text(team.get("name"), 60) or name, "matches": [self._af_match(i) for i in items]}

    async def _af_today(self) -> list[dict[str, object]]:
        async with self._client({"x-apisports-key": self._env("API_FOOTBALL_KEY")}) as client:
            items = (await self._get(client, f"{AF_BASE}/fixtures", date=self._today(), timezone="America/Fortaleza")).get("response")
        matches = [self._af_match(i) for i in items or [] if isinstance(i, dict)]
        return sorted(matches, key=lambda m: m["state"] not in LIVE_STATES)

    # ---------- TheSportsDB (fallback) ----------
    def _tsdb_match(self, item: dict[str, object], *, live: bool) -> dict[str, object]:
        status = str(item.get("strStatus") or "").strip().upper()
        has_score = item.get("intHomeScore") not in (None, "")
        progress = _text(item.get("strProgress"), 12)
        if live:
            state = "paused" if status == "HT" else "finished" if status in {"FT", "AET", "PEN", "FINISHED"} else "live"
        else:
            state = "finished" if has_score else "scheduled"
        stamp = item.get("strTimestamp")
        when = _parse_dt(f"{stamp}+00:00") if stamp else None
        return _match(
            league=_text(item.get("strLeague"), 80), home=_text(item.get("strHomeTeam"), 60), away=_text(item.get("strAwayTeam"), 60),
            homeLogoUrl=str(item.get("strHomeTeamBadge") or ""), awayLogoUrl=str(item.get("strAwayTeamBadge") or ""),
            homeScore=_score(item.get("intHomeScore")), awayScore=_score(item.get("intAwayScore")), state=state,
            minute=(f"{progress}'" if progress.isdigit() else "Intervalo" if state == "paused" else "Ao vivo" if state == "live" else ""),
            date=_text(item.get("dateEvent"), 10) or (when.strftime("%Y-%m-%d") if when else ""),
            time=when.strftime("%H:%M") if when else "", venue=_text(item.get("strVenue"), 80),
        )

    async def _tsdb_lookup(self, name: str) -> dict[str, object]:
        async with self._client() as client:
            teams = (await self._get(client, f"{TSDB_BASE}/searchteams.php", t=name)).get("teams")
            if not isinstance(teams, list) or not teams:
                return {"found": False}
            brazilian = [t for t in teams if isinstance(t, dict) and str(t.get("strCountry")) == "Brazil"]
            chosen = (brazilian or [t for t in teams if isinstance(t, dict)])[0]
            team_id, league_id = str(chosen.get("idTeam") or ""), str(chosen.get("idLeague") or "")
            last, nxt, live = await asyncio.gather(
                self._get(client, f"{TSDB_BASE}/eventslast.php", id=team_id),
                self._get(client, f"{TSDB_BASE}/eventsnext.php", id=team_id),
                self._get(client, f"{TSDB_BASE}/livescore.php", l=league_id) if league_id else asyncio.sleep(0, result={}),
                return_exceptions=True,
            )
        if all(isinstance(r, Exception) for r in (last, nxt, live)):
            raise ScoreError("sem_dados")
        matches: list[dict[str, object]] = []
        for payload, key, is_live in ((last, "results", False), (nxt, "events", False), (live, "livescore", True)):
            for item in (payload.get(key) if isinstance(payload, dict) else None) or []:
                if not isinstance(item, dict):
                    continue
                if is_live:
                    updated = _parse_dt(f"{str(item.get('updated') or '').replace(' ', 'T')}+00:00") if item.get("updated") else None
                    mine = team_id in {str(item.get("idHomeTeam")), str(item.get("idAwayTeam"))}
                    if not mine or item.get("dateEvent") != self._today() or not updated or self._now() - updated > timedelta(minutes=10):
                        continue
                matches.append(self._tsdb_match(item, live=is_live))
        return {"found": True, "team": _text(chosen.get("strTeam"), 60) or name, "matches": matches}

    async def _tsdb_today(self) -> list[dict[str, object]]:
        async with self._client() as client:
            events = (await self._get(client, f"{TSDB_BASE}/eventsday.php", d=self._today(), s="Soccer")).get("events")
        matches = [self._tsdb_match(e, live=False) for e in events or [] if isinstance(e, dict)]
        brazil = {_fold(e.get("strLeague")) for e in events or [] if isinstance(e, dict) and "brazil" in _fold(e.get("strLeague"))}
        return sorted(matches, key=lambda m: _fold(m["league"]) not in brazil)

    # ---------- orquestracao ----------
    async def lookup(self, team: str) -> dict[str, object]:
        name = canonical_team(team)
        if not name:
            return {"found": False}
        await ensure_tls()
        provider = self.provider
        if provider == "football-data.org":
            return await self._fd_lookup(name)
        if provider == "API-Football":
            return await self._af_lookup(name)
        return await self._tsdb_lookup(name)

    async def today_matches(self) -> list[dict[str, object]]:
        await ensure_tls()
        provider = self.provider
        if provider == "football-data.org":
            return await self._fd_today()
        if provider == "API-Football":
            return await self._af_today()
        return await self._tsdb_today()

    def _footer(self) -> dict[str, object]:
        return {"source": self.provider, "updatedAt": self._now().strftime("%H:%M"), "stale": self.stale}

    def build_team_props(self, data: dict[str, object]) -> tuple[dict[str, object], str]:
        """Aplica as regras de honestidade: so mostra como atual o que e de hoje."""
        today = self._today()
        matches = [m for m in data["matches"] if isinstance(m, dict)]
        live = next((m for m in matches if m["state"] in LIVE_STATES and m["date"] == today), None)
        todays = next((m for m in matches if m["date"] == today), None)
        team = str(data["team"])
        current = live or todays
        if current:
            summary = self._summary(current)
            props = {"state": current["state"], "team": team, "items": [current], **self._footer()}
        else:
            last = max((m for m in matches if m["state"] == "finished" and m["date"] < today), key=lambda m: m["date"], default=None)
            nxt = min((m for m in matches if m["state"] == "scheduled" and m["date"] > today), key=lambda m: m["date"], default=None)
            items = [{"label": f"Último jogo ({last['date'][8:]}/{last['date'][5:7]})", **last}] if last else []
            if nxt:
                items.append({"label": f"Próximo jogo ({nxt['date'][8:]}/{nxt['date'][5:7]} {nxt['time']})".strip(), **nxt})
            text = f"Sem jogo hoje para {team}."
            summary = text + (f" Próximo jogo: {nxt['home']} x {nxt['away']} em {nxt['date'][8:]}/{nxt['date'][5:7]} às {nxt['time']}." if nxt else "")
            props = {"state": "none", "team": team, "text": text, "items": items, **self._footer()}
        if self.stale:
            props["text"] = f"{props.get('text', '')} {STALE_NOTE}".strip()
            summary += " Aviso: fonte gratuita, pode estar desatualizado."
        return props, summary

    @staticmethod
    def _summary(match: dict[str, object]) -> str:
        minute = str(match["minute"])
        status = {"live": f"ao vivo, {minute}" if minute.endswith("'") else "ao vivo", "paused": "intervalo", "finished": "encerrado", "scheduled": f"hoje às {match['time']}"}[str(match["state"])]
        if match["state"] == "scheduled":
            return f"{match['home']} x {match['away']}, {status}."
        return f"{match['home']} {match['homeScore']} x {match['awayScore']} {match['away']}, {status}."

    async def run_score_job(self, team: str, *, publish: PUBLISHER) -> dict[str, object]:
        """Sempre devolve um resumo curto em 'summary', nunca fica pendente."""
        panel_id = score_panel_id(team)
        try:
            data = await asyncio.wait_for(self.lookup(team), TOTAL_TIMEOUT_SECONDS)
        except (ScoreError, asyncio.TimeoutError, httpx.HTTPError):
            await publish({"type": "status", "scope": "media", "state": "idle", "text": UNAVAILABLE_TEXT})
            return {"ok": False, "reason": "indisponivel", "summary": UNAVAILABLE_TEXT}
        if not data.get("found"):
            summary = f"Não encontrei o time {_safe_query(team, 60)}."
            await publish({"type": "status", "scope": "media", "state": "idle", "text": summary})
            return {"ok": False, "reason": "time_nao_encontrado", "summary": summary}
        props, summary = self.build_team_props(data)
        await publish({"type": "open_panel", "kind": "scoreboard", "id": panel_id, "title": "Placar", "props": props, "focus": True, "autoCloseMs": 600000})
        if props["state"] in LIVE_STATES:
            self._watch(panel_id, team, publish)
        return {"ok": True, "panel_id": panel_id, "state": props["state"], "summary": summary}

    async def run_today_job(self, *, publish: PUBLISHER) -> dict[str, object]:
        try:
            matches = await asyncio.wait_for(self.today_matches(), TOTAL_TIMEOUT_SECONDS)
        except (ScoreError, asyncio.TimeoutError, httpx.HTTPError):
            await publish({"type": "status", "scope": "media", "state": "idle", "text": UNAVAILABLE_TEXT})
            return {"ok": False, "reason": "indisponivel", "summary": UNAVAILABLE_TEXT}
        today = self._today()
        matches = [m for m in matches if m["date"] == today][:MAX_TODAY_ITEMS]
        if not matches:
            summary = "Não encontrei jogos hoje nas ligas disponíveis."
            props = {"state": "none", "text": summary, "items": [], **self._footer()}
        else:
            live = sum(1 for m in matches if m["state"] in LIVE_STATES)
            summary = f"{len(matches)} jogos hoje, {live} ao vivo. " + "; ".join(self._summary(m) for m in matches[:4])
            props = {"state": "live" if live else "scheduled", "text": "", "items": matches, **self._footer()}
        if self.stale:
            summary += " Aviso: fonte gratuita, pode estar desatualizado."
            props["text"] = STALE_NOTE
        await publish({"type": "open_panel", "kind": "scoreboard", "id": TODAY_PANEL_ID, "title": "Jogos de hoje", "props": props, "focus": True, "autoCloseMs": 600000})
        return {"ok": bool(matches), "panel_id": TODAY_PANEL_ID, "summary": summary}

    # ---------- atualizacao ao vivo ----------
    def _watch(self, panel_id: str, team: str, publish: PUBLISHER) -> None:
        self.stop_watching(panel_id)
        task = asyncio.ensure_future(self._watch_loop(panel_id, team, publish))
        self._watchers[panel_id] = task
        task.add_done_callback(lambda done, key=panel_id: self._watchers.pop(key, None) if self._watchers.get(key) is done else None)

    async def _watch_loop(self, panel_id: str, team: str, publish: PUBLISHER) -> None:
        deadline = asyncio.get_running_loop().time() + LIVE_WATCH_MAX_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(LIVE_REFRESH_SECONDS)
            try:
                data = await asyncio.wait_for(self.lookup(team), TOTAL_TIMEOUT_SECONDS)
                if not data.get("found"):
                    return
                props, _ = self.build_team_props(data)
            except (ScoreError, asyncio.TimeoutError, httpx.HTTPError):
                continue
            await publish({"type": "update_panel", "id": panel_id, "props": props})
            if props["state"] not in LIVE_STATES:
                return

    def stop_watching(self, panel_id: str) -> bool:
        task = self._watchers.pop(panel_id, None)
        if task and not task.done():
            task.cancel()
            return True
        return False

    async def shutdown(self) -> None:
        for panel_id in list(self._watchers):
            self.stop_watching(panel_id)


__all__ = ["LiveScoreService", "TODAY_PANEL_ID", "canonical_team", "score_panel_id"]
