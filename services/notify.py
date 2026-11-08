"""Avisos para fora da Kira (celular, Telegram, Discord, e-mail, WhatsApp...) pelo Apprise.

Cada destino é um endereço do Apprise (ex.: ntfy://meu-topico, tgram://token/chat, link de webhook do Discord,
mailtos://usuario:senha-de-app@gmail.com). Os endereços levam chaves: ficam no cofre (configs/secure, DPAPI) e nunca
saem daqui; a tela e a Kira só veem o nome e o tipo.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
import uuid
from typing import Any

from services.secure_store import SecureStore, SecureStoreUnavailable

logger = logging.getLogger("kira.app.notify")

STORE_NAME = "notify_destinations"
MAX_DESTINATIONS = 10
SEND_TIMEOUT_S = 20.0
MAX_BODY = 1500


class NotifyError(ValueError):
    pass


def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", folded).split())


def service_of(url: str) -> str | None:
    """Tipo do destino ('Telegram', 'Discord', 'ntfy'...), ou None se o Apprise não entende o endereço."""
    import apprise  # ~0,4 s: só quando usado

    plugin = apprise.Apprise.instantiate(str(url or "").strip())
    return str(getattr(plugin, "service_name", "") or type(plugin).__name__) if plugin else None


def _send_one(url: str, title: str, body: str, attach: str | None = None) -> bool:
    import apprise

    target = apprise.Apprise()
    if not target.add(url):
        return False
    return bool(target.notify(title=title, body=body, attach=attach))


class Notifier:
    def __init__(self, store: SecureStore | None = None, *, sender=_send_one, timeout_s: float = SEND_TIMEOUT_S,
                 push: Any = None) -> None:
        self._store = store or SecureStore()
        self._send = sender
        self._timeout = timeout_s
        # app da Kira no celular (services/web_push.py): é o "celular" quando o Chefe não cadastrou outro com esse nome
        self._push = push
        self.mirror = None  # (título, texto) -> None: kira_app mostra o aviso na TV quando a Kira está lá

    def _load(self) -> list[dict[str, str]]:
        data = self._store.load(STORE_NAME) or {}
        items = data.get("destinations")
        return [item for item in items if isinstance(item, dict) and item.get("url")] if isinstance(items, list) else []

    def _save(self, items: list[dict[str, str]]) -> None:
        self._store.save(STORE_NAME, {"destinations": items})

    def destinations(self) -> list[dict[str, str]]:
        """Para a tela e para a Kira: sem o endereço."""
        return [{"id": item["id"], "name": item["name"], "kind": item.get("kind", "")} for item in self._load()]

    def add(self, name: str, url: str) -> dict[str, str]:
        name = " ".join(str(name or "").split())[:40]
        url = str(url or "").strip()
        if not name or not url:
            raise NotifyError("Dê um nome e cole o endereço.")
        if len(url) > 1000:
            raise NotifyError("Endereço grande demais.")
        kind = service_of(url)
        if kind is None:
            raise NotifyError("Endereço não reconhecido. Veja os exemplos abaixo.")
        items = self._load()
        if len(items) >= MAX_DESTINATIONS:
            raise NotifyError(f"No máximo {MAX_DESTINATIONS} destinos.")
        if any(_plain(item["name"]) == _plain(name) for item in items):
            raise NotifyError("Já existe um destino com esse nome.")
        item = {"id": uuid.uuid4().hex[:10], "name": name, "kind": kind, "url": url}
        try:
            self._save([*items, item])
        except SecureStoreUnavailable as exc:
            raise NotifyError("O cofre do Windows não está disponível.") from exc
        logger.info("NOTIFY_DESTINATION_ADDED kind=%s", kind)
        return {"id": item["id"], "name": name, "kind": kind}

    def remove(self, destination_id: str) -> bool:
        items = self._load()
        kept = [item for item in items if item["id"] != destination_id]
        if len(kept) == len(items):
            return False
        self._save(kept)
        return True

    def pick(self, to: str = "") -> list[dict[str, str]]:
        """'' ou 'todos' = todos; senão os destinos cujo nome ou tipo aparece no pedido ('no telegram', 'celular')."""
        items = self._load()
        wanted = _plain(to)
        if not wanted or wanted in ("todos", "tudo", "todos os destinos"):
            return items
        by_id = [item for item in items if item["id"] == to]
        if by_id:
            return by_id
        chosen = [item for item in items if _plain(item["name"]) in wanted or wanted in _plain(item["name"])]
        if not chosen:
            chosen = [item for item in items if _plain(item.get("kind", "")) and _plain(item.get("kind", "")) in wanted]
        return chosen

    async def send(self, body: str, *, title: str = "Kira", to: str = "", attach: str | None = None) -> dict[str, Any]:
        """attach: caminho de uma foto (ex.: a do vigia); destinos que não aceitam anexo recebem só o texto."""
        body = str(body or "").strip()[:MAX_BODY]
        if not body:
            return {"ok": False, "reason": "vazio", "sent": [], "failed": []}
        if self.mirror is not None:
            try:
                self.mirror(title, body)
            except Exception:  # noqa: BLE001 - a TV é extra: nunca atrapalha o aviso
                pass
        targets = self.pick(to)
        wanted = _plain(to)
        app_sent: list[str] = []
        if self._push is not None and self._push.count() and (not wanted or not targets or any(
                word in wanted for word in ("celular", "app", "kira", "todos", "tudo", "telefone"))):
            pushed = await self._push.send(title, body)
            app_sent = ["App da Kira no celular"] if pushed["sent"] else []
            # O celular é o site da Kira (2026-10-31: "esqueça o ntfy, é pelo site"): o destino antigo chamado "Celular"
            # só recebe se o push do site falhou ou se ele foi pedido pelo tipo/nome exato que não é "celular".
            if app_sent and any(word in wanted for word in ("celular", "telefone", "app")):
                targets = [item for item in targets if not any(word in _plain(item["name"]) for word in ("celular", "telefone"))
                           and _plain(item.get("kind", "")) != "ntfy"]
        if not targets and app_sent:
            return {"ok": True, "sent": app_sent, "failed": []}
        if not targets:
            return {"ok": False, "reason": "sem_destino" if not self._load() else "destino_nao_encontrado", "sent": [], "failed": [],
                    "available": [item["name"] for item in self._load()]}

        async def one(item: dict[str, str]) -> bool:
            try:
                args = (item["url"], str(title)[:120], body) + ((attach,) if attach else ())
                return await asyncio.wait_for(asyncio.to_thread(self._send, *args), self._timeout)
            except Exception as exc:  # um destino fora do ar não derruba os outros
                logger.info("NOTIFY_SEND_FAILED kind=%s reason=%s", item.get("kind", ""), type(exc).__name__)
                return False

        results = await asyncio.gather(*(one(item) for item in targets))
        sent = app_sent + [item["name"] for item, ok in zip(targets, results) if ok]
        failed = [item["name"] for item, ok in zip(targets, results) if not ok]
        logger.info("NOTIFY_SENT sent=%d failed=%d", len(sent), len(failed))
        return {"ok": bool(sent), "sent": sent, "failed": failed}


__all__ = ["MAX_DESTINATIONS", "NotifyError", "Notifier", "service_of"]
