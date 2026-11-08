"""OAuth do Google para app de computador: PKCE + retorno em loopback (127.0.0.1) no próprio backend da Kira.

Credenciais do app só por variável de ambiente (KIRA_GOOGLE_CLIENT_ID / KIRA_GOOGLE_CLIENT_SECRET); o token do usuário
fica criptografado no SecureStore. Escopos somente leitura.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from services.secure_store import SecureStore

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = {
    "calendar": "https://www.googleapis.com/auth/calendar.readonly",
    "email": "https://www.googleapis.com/auth/gmail.readonly",
}
TIMEOUT = httpx.Timeout(6.0, connect=3.0)
PENDING_SECONDS = 600.0


class NotConnected(RuntimeError):
    """Sem credencial do app, sem consentimento do usuário ou token revogado."""


APP_KEY = "google_app"  # credencial do app colada pelo Chefe em Configurações (criptografada), se não houver no ambiente


def client_credentials(store: SecureStore | None = None) -> tuple[str, str] | None:
    client_id = os.getenv("KIRA_GOOGLE_CLIENT_ID", "").strip()
    secret = os.getenv("KIRA_GOOGLE_CLIENT_SECRET", "").strip()
    if not (client_id and secret) and store is not None:
        saved = store.load(APP_KEY) or {}
        client_id, secret = str(saved.get("client_id") or "").strip(), str(saved.get("client_secret") or "").strip()
    return (client_id, secret) if client_id and secret else None


class GoogleAuth:
    def __init__(self, service: str, store: SecureStore, *, redirect_uri: str, transport: httpx.AsyncBaseTransport | None = None, clock=time.time) -> None:
        if service not in SCOPES:
            raise ValueError("servico_desconhecido")
        self._service = service
        self._store = store
        self._redirect = redirect_uri
        self._transport = transport
        self._clock = clock
        self._pending: dict[str, tuple[str, float]] = {}

    @property
    def connected(self) -> bool:
        return bool((self._store.load(f"google_{self._service}") or {}).get("refresh_token"))

    def authorization_url(self) -> str:
        credentials = client_credentials(self._store)
        if credentials is None:
            raise NotConnected("falta a credencial do Google: veja Configurações > Kira > Recursos > Como conectar (ou pergunte à Kira)")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        self._pending = {k: v for k, v in self._pending.items() if self._clock() - v[1] < PENDING_SECONDS}
        self._pending[state] = (verifier, self._clock())
        return AUTH_URL + "?" + urlencode({
            "client_id": credentials[0], "redirect_uri": self._redirect, "response_type": "code", "scope": SCOPES[self._service],
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "access_type": "offline", "prompt": "consent",
        })

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
            response = await client.post(TOKEN_URL, data=data)
        if response.status_code != 200:
            raise NotConnected("o Google recusou a autorização")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise NotConnected("resposta de autorização inválida")
        return payload

    async def complete(self, state: str, code: str) -> None:
        pending = self._pending.pop(state, None)
        credentials = client_credentials(self._store)
        if pending is None or credentials is None or self._clock() - pending[1] > PENDING_SECONDS:
            raise NotConnected("pedido de autorização expirado ou desconhecido")
        payload = await self._token_request({
            "client_id": credentials[0], "client_secret": credentials[1], "code": code, "code_verifier": pending[0],
            "grant_type": "authorization_code", "redirect_uri": self._redirect,
        })
        if not payload.get("refresh_token"):
            raise NotConnected("o Google não devolveu acesso permanente")
        self._save(payload, payload["refresh_token"])

    def _save(self, payload: dict[str, Any], refresh_token: str) -> None:
        self._store.save(f"google_{self._service}", {
            "access_token": payload["access_token"], "refresh_token": refresh_token,
            "expires_at": self._clock() + float(payload.get("expires_in") or 3000) - 60,
        })

    async def access_token(self) -> str:
        token = self._store.load(f"google_{self._service}")
        if not token or not token.get("refresh_token"):
            raise NotConnected("conta Google não conectada")
        if float(token.get("expires_at") or 0) > self._clock():
            return str(token["access_token"])
        credentials = client_credentials(self._store)
        if credentials is None:
            raise NotConnected("credencial do app Google ausente")
        payload = await self._token_request({
            "client_id": credentials[0], "client_secret": credentials[1], "refresh_token": str(token["refresh_token"]), "grant_type": "refresh_token",
        })
        self._save(payload, str(token["refresh_token"]))
        return str(payload["access_token"])

    def revoke_local(self) -> bool:
        return self._store.delete(f"google_{self._service}")


__all__ = ["GoogleAuth", "NotConnected", "SCOPES", "client_credentials"]
