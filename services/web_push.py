"""Notificação no celular pelo app da Kira instalado pelo Chrome (Web Push).

O celular (página aberta pelo portão, HTTPS) pede permissão, se inscreve com a chave pública da Kira e a inscrição fica
no cofre (configs/secure, DPAPI). Para avisar, o backend manda a mensagem pelo serviço de push do navegador (Google para
o Chrome); o service worker do app (interface/sw.js) mostra a notificação mesmo com o app fechado.
Chave VAPID gerada aqui na primeira vez, também no cofre. Inscrição recusada pelo serviço (404/410) sai da lista.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from services.secure_store import SecureStore

logger = logging.getLogger("kira.app.push")

KEY_NAME = "webpush_vapid"
SUBS_NAME = "webpush_subscriptions"
MAX_SUBSCRIPTIONS = 12
SUBJECT = "https://kira.local"  # identifica quem manda; nada pessoal (o e-mail do Chefe não vai a lugar nenhum)
TTL_S = 6 * 3600
SEND_TIMEOUT_S = 15.0


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _send_default(subscription: dict[str, Any], payload: str, private_pem: str) -> int:
    from py_vapid import Vapid01
    from pywebpush import WebPushException, webpush

    try:
        response = webpush(subscription_info=subscription, data=payload, vapid_private_key=Vapid01.from_pem(private_pem.encode("ascii")),
                           vapid_claims={"sub": SUBJECT}, ttl=TTL_S, timeout=SEND_TIMEOUT_S)
        return int(getattr(response, "status_code", 201) or 201)
    except WebPushException as exc:
        return int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)


class WebPush:
    def __init__(self, store: SecureStore | None = None, *, sender: Callable[[dict[str, Any], str, str], int] = _send_default,
                 clock: Callable[[], float] = time.time) -> None:
        self._store = store or SecureStore()
        self._send = sender
        self._clock = clock

    def _keys(self) -> dict[str, str]:
        data = self._store.load(KEY_NAME) or {}
        if data.get("private_pem") and data.get("public"):
            return data
        from cryptography.hazmat.primitives import serialization
        from py_vapid import Vapid01

        vapid = Vapid01()
        vapid.generate_keys()
        public = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        private = vapid.private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        data = {"private_pem": private.decode("ascii"), "public": _b64url(public)}
        self._store.save(KEY_NAME, data)
        logger.info("WEBPUSH_KEYS_CREATED")
        return data

    def public_key(self) -> str:
        return self._keys()["public"]

    def _subscriptions(self) -> list[dict[str, Any]]:
        items = (self._store.load(SUBS_NAME) or {}).get("items")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def count(self, profile: str | None = None) -> int:
        return sum(1 for item in self._subscriptions() if profile is None or item.get("profile") == profile)

    def subscribe(self, raw: Any, *, profile: str = "dono", label: str = "") -> bool:
        if not isinstance(raw, dict):
            return False
        endpoint = str(raw.get("endpoint") or "")
        keys = raw.get("keys") if isinstance(raw.get("keys"), dict) else {}
        p256dh, auth = str(keys.get("p256dh") or ""), str(keys.get("auth") or "")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or len(endpoint) > 1000 or not (20 <= len(p256dh) <= 200) or not (8 <= len(auth) <= 100):
            return False
        items = [item for item in self._subscriptions() if item.get("endpoint") != endpoint]
        items.append({"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}, "profile": str(profile)[:40],
                      "label": " ".join(str(label).split())[:60], "at": int(self._clock())})
        self._store.save(SUBS_NAME, {"items": items[-MAX_SUBSCRIPTIONS:]})
        logger.info("WEBPUSH_SUBSCRIBED total=%d", min(len(items), MAX_SUBSCRIPTIONS))
        return True

    def unsubscribe(self, endpoint: str) -> bool:
        items = self._subscriptions()
        kept = [item for item in items if item.get("endpoint") != endpoint]
        if len(kept) != len(items):
            self._store.save(SUBS_NAME, {"items": kept})
        return len(kept) != len(items)

    async def send(self, title: str, body: str, *, url: str = "/", profile: str | None = "dono", tag: str = "") -> dict[str, int]:
        targets = [item for item in self._subscriptions() if profile is None or item.get("profile") == profile]
        if not targets:
            return {"sent": 0, "failed": 0}
        payload = json.dumps({"title": str(title)[:80], "body": str(body)[:300], "url": url if str(url).startswith("/") else "/",
                              "tag": str(tag)[:40]}, ensure_ascii=False)
        private = self._keys()["private_pem"]

        async def one(item: dict[str, Any]) -> int:
            subscription = {"endpoint": item["endpoint"], "keys": item["keys"]}
            try:
                return await asyncio.wait_for(asyncio.to_thread(self._send, subscription, payload, private), SEND_TIMEOUT_S + 5)
            except Exception as exc:  # um aparelho fora do ar não segura os outros
                logger.info("WEBPUSH_SEND_FAILED reason=%s", type(exc).__name__)
                return 0

        codes = await asyncio.gather(*(one(item) for item in targets))
        for item, code in zip(targets, codes):
            if code in (404, 410):  # o celular desinstalou ou tirou a permissão
                self.unsubscribe(item["endpoint"])
        sent = sum(1 for code in codes if 200 <= code < 300)
        logger.info("WEBPUSH_SENT sent=%d failed=%d", sent, len(codes) - sent)
        return {"sent": sent, "failed": len(codes) - sent}


__all__ = ["WebPush"]
