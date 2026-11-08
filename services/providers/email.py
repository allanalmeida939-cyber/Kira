"""E-mail do Gmail (somente leitura): metadados e trecho original, deduplicados e classificados por regras explícitas.

Conteúdo de e-mail é dado não confiável: nunca autoriza ferramenta nem muda regra. O "resumo" do painel é o trecho
original marcado como tal; qualquer resumo feito pela Kira é falado por ela e identificado como dela.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from services.media_topics import fold
from services.providers.google_oauth import GoogleAuth, NotConnected
from services.providers.news import plain_text

LIST_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
TIMEOUT = httpx.Timeout(6.0, connect=3.0)
URGENT_RE = re.compile(r"\b(urgente|imediato|hoje|prazo|vence|vencimento|ultimo dia|bloqueio|suspens|atrasad|cobranca)\b")
ACTION_RE = re.compile(r"\b(responda|confirme|pague|assine|envie|preencha|acesse|aprovar|aprovacao|confirmar|pendente)\b")
DEADLINE_RE = re.compile(r"\b(ate|vence em|prazo)\s+(?:o dia\s+)?(\d{1,2}/\d{1,2}(?:/\d{2,4})?)")
CATEGORIES = (("financeiro", re.compile(r"\b(fatura|boleto|pagamento|banco|pix|cobranca|nota fiscal)\b")),
              ("trabalho", re.compile(r"\b(reuniao|projeto|relatorio|entrega|cliente|contrato)\b")),
              ("compras", re.compile(r"\b(pedido|entrega|rastreio|compra|frete)\b")),
              ("seguranca", re.compile(r"\b(senha|login|acesso|codigo de verificacao|seguranca)\b")))


class EmailUnavailable(RuntimeError):
    pass


@dataclass
class EmailMessage:
    messageId: str
    sender: str
    subject: str
    receivedAt: str | None
    snippet: str
    priority: str
    category: str
    deadline: str | None
    actionItems: list[str] = field(default_factory=list)
    requiresUserAction: bool = False
    sourceLink: str = ""


def classify(subject: str, snippet: str) -> dict[str, Any]:
    text = fold(f"{subject} {snippet}")
    deadline = DEADLINE_RE.search(text)
    actions = sorted({m.group(1) for m in ACTION_RE.finditer(text)})
    return {
        "priority": "alta" if URGENT_RE.search(text) else ("média" if actions else "baixa"),
        "category": next((name for name, pattern in CATEGORIES if pattern.search(text)), "geral"),
        "deadline": deadline.group(2) if deadline else None,
        "actionItems": actions[:4],
        "requiresUserAction": bool(actions),
    }


def normalize_message(raw: dict[str, Any]) -> EmailMessage | None:
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    headers = {str(h.get("name") or "").lower(): str(h.get("value") or "") for h in (raw.get("payload") or {}).get("headers") or [] if isinstance(h, dict)}
    subject = plain_text(headers.get("subject") or "(sem assunto)", 160)
    snippet = plain_text(raw.get("snippet"), 240)
    try:
        received = parsedate_to_datetime(headers["date"]).isoformat() if headers.get("date") else None
    except (TypeError, ValueError):
        received = None
    message_id = str(raw["id"])[:64]
    return EmailMessage(
        messageId=message_id, sender=plain_text(headers.get("from"), 120), subject=subject, receivedAt=received, snippet=snippet,
        sourceLink=f"https://mail.google.com/mail/u/0/#inbox/{message_id}", **classify(subject, snippet),
    )


class EmailProvider:
    def __init__(self, auth: GoogleAuth, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._auth = auth
        self._transport = transport

    async def recent(self, *, query: str = "newer_than:2d", limit: int = 15) -> list[EmailMessage]:
        try:
            token = await self._auth.access_token()
        except NotConnected as exc:
            raise EmailUnavailable(str(exc)) from exc
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport, headers=headers) as client:
                listing = await client.get(LIST_URL, params={"q": query, "maxResults": str(limit)})
                if listing.status_code != 200:
                    raise EmailUnavailable("o Gmail recusou a consulta")
                ids = list(dict.fromkeys(str(m.get("id")) for m in (listing.json().get("messages") or []) if isinstance(m, dict) and m.get("id")))
                details = await asyncio.gather(*(
                    client.get(f"{LIST_URL}/{mid}", params=[("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"), ("metadataHeaders", "Date")])
                    for mid in ids[:limit]
                ), return_exceptions=True)
        except httpx.HTTPError as exc:
            raise EmailUnavailable("o Gmail não respondeu") from exc
        messages = [normalize_message(d.json()) for d in details if isinstance(d, httpx.Response) and d.status_code == 200]
        ordered = [m for m in messages if m is not None]
        ordered.sort(key=lambda m: datetime.fromisoformat(m.receivedAt).timestamp() if m.receivedAt else 0.0, reverse=True)
        ordered.sort(key=lambda m: {"alta": 0, "média": 1, "baixa": 2}[m.priority])
        return ordered


def as_dicts(messages: list[EmailMessage]) -> list[dict[str, Any]]:
    return [asdict(m) for m in messages]


__all__ = ["EmailMessage", "EmailProvider", "EmailUnavailable", "as_dicts", "classify", "normalize_message"]
