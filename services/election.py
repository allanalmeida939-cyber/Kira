"""Resultados de eleicao a partir dos arquivos publicos do TSE (resultados.tse.jus.br), sem chave."""

from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from datetime import datetime
from typing import Callable

import httpx

from services.image_proxy import proxied_url
from services.media_search import PUBLISHER, _text
from services.shared_http import ensure_tls

BASE = "https://resultados.tse.jus.br/oficial"
TOTAL_TIMEOUT_SECONDS = 8.0
CONFIG_TTL_SECONDS = 300.0
MAX_CANDIDATES = 8
PANEL_ID = "election-{slug}"
UNAVAILABLE = "Não consegui os resultados da eleição agora."
UFS = {
    "ac": "acre", "al": "alagoas", "ap": "amapa", "am": "amazonas", "ba": "bahia", "ce": "ceara", "df": "distrito federal",
    "es": "espirito santo", "go": "goias", "ma": "maranhao", "mt": "mato grosso", "ms": "mato grosso do sul", "mg": "minas gerais",
    "pa": "para", "pb": "paraiba", "pr": "parana", "pe": "pernambuco", "pi": "piaui", "rj": "rio de janeiro", "rn": "rio grande do norte",
    "rs": "rio grande do sul", "ro": "rondonia", "rr": "roraima", "sc": "santa catarina", "sp": "sao paulo", "se": "sergipe", "to": "tocantins",
}
DEFAULT_UF = "ce"
CARGOS = {"presidente": (1, "br"), "governador": (3, None), "senador": (5, None)}


class ElectionError(Exception):
    """Fonte do TSE indisponivel ou sem dados para o cargo pedido."""


def _fold(value: object) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()


def parse_race(text: str) -> tuple[str, int, str]:
    """('presidente'|'governador'|'senador', codigo do cargo, UF) a partir de texto livre."""
    folded = _fold(text)
    name = next((k for k in CARGOS if k in folded or k[:-1] + "a" in folded), "presidente")
    code, fixed_uf = CARGOS[name]
    if fixed_uf:
        return name, code, fixed_uf
    for uf, label in sorted(UFS.items(), key=lambda kv: -len(kv[1])):
        if re.search(rf"\b{label}\b", folded):
            return name, code, uf
    for uf in UFS:
        if re.search(rf"\b{uf}\b", folded):
            return name, code, uf
    return name, code, DEFAULT_UF


def _pct(value: object) -> float | None:
    """Percentual do TSE; ausente ou ilegível fica None (nunca vira zero nem é estimado)."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def check_consistency(rows: list[dict[str, object]], valid_votes: int) -> str | None:
    """Confere percentuais contra votos e votos válidos antes de qualquer gráfico. Devolve o problema ou None."""
    pcts = [r["pct"] for r in rows if isinstance(r.get("pct"), (int, float))]
    if any(p < 0 or p > 100 for p in pcts) or sum(pcts) > 100.05:
        return "percentuais somam mais de 100%"
    if valid_votes > 0:
        if sum(int(r.get("votes") or 0) for r in rows) > valid_votes:
            return "votos dos candidatos passam do total de votos válidos"
        for row in rows:
            if isinstance(row.get("pct"), (int, float)) and abs(int(row.get("votes") or 0) * 100 / valid_votes - float(row["pct"])) > 0.1:
                return "percentual não bate com os votos"
    return None


def _title(name: str) -> str:
    small = {"da", "de", "do", "dos", "das", "e"}
    return " ".join(w if w in small else w.capitalize() for w in name.lower().split())


class ElectionService:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, now: Callable[[], datetime] | None = None, clock=time.monotonic) -> None:
        self._transport = transport
        self._now = now or datetime.now
        self._clock = clock
        self._config: tuple[float, dict[str, object]] | None = None

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> dict[str, object]:
        try:
            response = await client.get(url)
            response.raise_for_status()
            payload = json.loads(response.content)
        except (httpx.HTTPError, ValueError) as exc:
            raise ElectionError(type(exc).__name__) from exc
        return payload if isinstance(payload, dict) else {}

    async def _candidates_elections(self, client: httpx.AsyncClient, kind: str) -> list[tuple[str, str, str, int]]:
        """Eleicoes ordinarias do tipo ('federal'|'estadual'), da mais recente para a mais antiga: (ciclo, codigo, nome, turno)."""
        if self._config is None or self._clock() - self._config[0] > CONFIG_TTL_SECONDS:
            self._config = (self._clock(), await self._get_json(client, f"{BASE}/comum/config/ele-c.json"))
        rows: list[tuple[datetime, int, str, str, str]] = []
        for pleito in self._config[1].get("pl") or []:
            if not isinstance(pleito, dict) or not str(pleito.get("c") or "").startswith("ele"):
                continue
            try:
                day = datetime.strptime(str(pleito.get("dt")), "%d/%m/%Y")
            except ValueError:
                continue
            if day > self._now().replace(tzinfo=None):
                continue
            for election in pleito.get("e") or []:
                name = _fold(election.get("nm"))
                if isinstance(election, dict) and "ordinaria" in name and kind in name:
                    rows.append((day, int(election.get("t") or 1), str(pleito["c"]), str(election.get("cd")), str(election.get("nm"))))
        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
        return [(cycle, code, name, turn) for _, turn, cycle, code, name in rows]

    async def fetch(self, race: str) -> dict[str, object]:
        name, cargo, uf = parse_race(race)
        kind = "federal" if name == "presidente" else "estadual"
        verify = await ensure_tls()
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0), headers={"User-Agent": "KiraAssistant/8.0 python-httpx"}, transport=self._transport, verify=verify) as client:
            for cycle, code, _, turn in await self._candidates_elections(client, kind):
                url = f"{BASE}/{cycle}/{code}/dados/{uf}/{uf}-c{cargo:04d}-e{int(code):06d}-u.json"
                try:
                    data = await self._get_json(client, url)
                except ElectionError:
                    continue
                parsed = self._parse(data, cargo=cargo, cargo_name=name, uf=uf, turn=turn, cycle=cycle, code=code)
                if parsed["candidates"]:
                    return parsed
        raise ElectionError("sem_dados")

    def _parse(self, data: dict[str, object], *, cargo: int, cargo_name: str, uf: str, turn: int, cycle: str, code: str) -> dict[str, object]:
        section = next((c for c in data.get("carg") or [] if isinstance(c, dict) and str(c.get("cd")) == str(cargo)), None) or {}
        rows = []
        for agr in section.get("agr") or []:
            for party in agr.get("par") or []:
                for cand in party.get("cand") or []:
                    votes = int(re.sub(r"\D", "", str(cand.get("vap") or "")) or 0)
                    photo = proxied_url(f"{BASE}/{cycle}/{code}/fotos/{uf}/{cand.get('sqcand')}.jpeg") if cand.get("sqcand") else ""
                    rows.append({
                        "name": _title(_text(cand.get("nmu") or cand.get("nm"), 60)), "party": _text(party.get("sg"), 20),
                        "number": _text(cand.get("n"), 5), "votes": votes, "pct": _pct(cand.get("pvap")),
                        "elected": str(cand.get("e")) == "s", "status": _text(cand.get("st"), 30), **({"photoUrl": photo} if photo else {}),
                    })
        rows.sort(key=lambda r: -int(r["votes"]))
        counted = _pct((data.get("s") or {}).get("pst"))
        valid_votes = int(re.sub(r"\D", "", str((data.get("v") or {}).get("vv") or "")) or 0)
        status = "apuração concluída" if counted is not None and counted >= 100 else "apuração em andamento"
        return {
            "kind": "official_result", "cargo": cargo_name, "uf": uf, "turn": turn, "apurado": counted, "status": status,
            "candidates": rows[:MAX_CANDIDATES], "validVotes": valid_votes, "problem": check_consistency(rows, valid_votes),
            "updatedAt": f"{_text(data.get('dg'), 10)[:5]} {_text(data.get('hg'), 8)[:5]}".strip(), "source": "TSE",
            "queriedAt": self._now().strftime("%d/%m %H:%M"),
        }

    @staticmethod
    def summary(result: dict[str, object]) -> str:
        label = {"presidente": "Presidente", "governador": f"Governador ({str(result['uf']).upper()})", "senador": f"Senador ({str(result['uf']).upper()})"}[str(result["cargo"])]
        if result.get("problem"):
            return f"{label}: os números do TSE vieram incoerentes agora ({result['problem']}); não vou citar percentuais."
        pct = lambda value: "sem percentual" if value is None else f"{format(float(value), '.2f').replace('.', ',')}%"
        top = "; ".join(f"{c['name']} ({c['party']}) {pct(c['pct'])}" for c in result["candidates"][:4])  # type: ignore[index]
        counted = "apuração sem percentual informado" if result["apurado"] is None else f"{pct(result['apurado'])} das urnas apuradas"
        return f"Resultado oficial do TSE. {label}, {result['turn']}º turno, {result['status']}, {counted}, atualizado {result['updatedAt']}, consultado {result['queriedAt']}. {top}."

    async def run_job(self, race: str, *, publish: PUBLISHER) -> dict[str, object]:
        try:
            result = await asyncio.wait_for(self.fetch(race), TOTAL_TIMEOUT_SECONDS)
        except (ElectionError, asyncio.TimeoutError, httpx.HTTPError):
            await publish({"type": "status", "scope": "media", "state": "idle", "text": UNAVAILABLE})
            return {"ok": False, "reason": "indisponivel", "summary": UNAVAILABLE}
        label = {"presidente": "Presidente", "governador": f"Governador {str(result['uf']).upper()}", "senador": f"Senador {str(result['uf']).upper()}"}[str(result["cargo"])]
        panel_id = PANEL_ID.format(slug=f"{result['cargo']}-{result['uf']}")
        props = {
            "title": f"{label} · {result['turn']}º turno", "apurado": result["apurado"], "items": result["candidates"],
            "updatedAt": result["updatedAt"], "source": "TSE", "status": result["status"], "kind": "Resultado oficial",
            "date": result["queriedAt"], **({"summary": f"Números incoerentes: {result['problem']}"} if result.get("problem") else {}),
        }
        await publish({"type": "open_panel", "kind": "election", "id": panel_id, "title": "Eleição", "props": props, "focus": True, "autoCloseMs": 600000})
        return {"ok": True, "panel_id": panel_id, "summary": self.summary(result)}


__all__ = ["ElectionError", "ElectionService", "PANEL_ID", "parse_race"]
