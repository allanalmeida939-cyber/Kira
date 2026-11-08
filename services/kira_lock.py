"""Bloqueio da Kira (Fase 5). NÃO substitui o bloqueio do Windows: só cobre a Kira (painéis e comandos).

"bloquear a Kira" -> tela própria; só abre com senha (PBKDF2-SHA256, sal aleatório, 600 mil rodadas, em runtime/,
fora do git). Voz sozinha nunca desbloqueia. Tentativas erradas: espera crescente. Enquanto trancada existe
runtime/kira_locked; o agente recusa ferramentas nesse estado. "bloquear o PC" usa /api/security/lock (LockWorkStation).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.local_request import is_local_request

ROOT = Path(__file__).resolve().parents[1]
SECRET = ROOT / "runtime" / "kira_lock.json"
FLAG = ROOT / "runtime" / "kira_locked"
ROUNDS = 600_000


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ROUNDS).hex()


class KiraLock:
    def __init__(self, secret: Path = SECRET, flag: Path = FLAG, clock: Callable[[], float] = time.monotonic) -> None:
        self.secret, self.flag, self.clock = secret, flag, clock
        self.fails = 0
        self.wait_until = 0.0

    @property
    def locked(self) -> bool:
        return self.flag.exists()

    @property
    def has_password(self) -> bool:
        return self.secret.exists()

    def set_password(self, new: str, old: str = "") -> str | None:
        if not re.fullmatch(r"[A-Za-z]{4,}", new):  # trancada, só as letras passam pelo teclado (kira_lock_guard)
            return "Use só letras, sem acento (mínimo 4): no bloqueio números e símbolos ficam travados."
        if self.has_password and not self._check(old):
            return "Senha atual errada."
        salt = secrets.token_bytes(16)
        self.secret.parent.mkdir(parents=True, exist_ok=True)
        self.secret.write_text(json.dumps({"salt": salt.hex(), "hash": _hash(new, salt), "rounds": ROUNDS}), encoding="utf-8")
        return None

    def _check(self, password: str) -> bool:
        try:
            data = json.loads(self.secret.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return hmac.compare_digest(_hash(password, bytes.fromhex(data["salt"])), data["hash"])

    def lock(self) -> None:
        self.flag.parent.mkdir(parents=True, exist_ok=True)
        self.flag.write_text(str(time.time()), encoding="utf-8")

    def unlock(self, password: str) -> dict[str, Any]:
        wait = self.wait_until - self.clock()
        if wait > 0:
            return {"ok": False, "error": f"Muitas tentativas. Espere {int(wait) + 1} s."}
        if not self._check(password):
            self.fails += 1
            if self.fails >= 3:  # 3 erros: 30 s, depois dobra a cada erro (até 15 min)
                self.wait_until = self.clock() + min(900, 30 * 2 ** (self.fails - 3))
            return {"ok": False, "error": "Senha errada."}
        self.fails, self.wait_until = 0, 0.0
        self.flag.unlink(missing_ok=True)
        return {"ok": True}


def create_lock_router(lock: KiraLock, *, token_provider: Callable[[], str], run_js: Callable[[str], Any],
                       on_change: Callable[[bool], Any] | None = None) -> APIRouter:
    """on_change(True/False): tela cheia, telas dos outros monitores, teclado/mouse presos e TV (kira_app).
    A tela trancada consulta /api/lock/state a cada poucos segundos: isso é o sinal de vida do bloqueio forte."""
    router = APIRouter()

    def changed(on: bool) -> None:
        if on_change is not None:
            try:
                on_change(on)
            except Exception:  # noqa: BLE001 - o bloqueio simples continua valendo
                pass

    def allow(request: Request, token: str | None) -> None:
        if not (is_local_request(request) or (token and hmac.compare_digest(token, token_provider() or "-"))):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    @router.get("/api/lock/state")
    async def state(request: Request) -> dict[str, Any]:
        allow(request, None)
        if lock.locked:
            changed(True)  # sinal de vida (e religa tudo se a Kira reiniciou trancada)
        return {"ok": True, "locked": lock.locked, "has_password": lock.has_password}

    @router.post("/api/lock/{action}")
    async def act(action: str, request: Request, body: dict = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        if action == "lock":
            if not lock.has_password:
                try:
                    await run_js("window.KiraLock && window.KiraLock.setup()")
                except Exception:  # noqa: BLE001
                    pass
                return {"ok": False, "error": "sem_senha"}
            lock.lock()
            changed(True)
            try:
                await run_js("window.KiraLock && window.KiraLock.show()")
            except Exception:  # noqa: BLE001 - a tela também lê o estado ao carregar
                pass
            return {"ok": True}
        if action == "unlock":
            result = lock.unlock(str(body.get("password") or ""))
            if result.get("ok"):
                changed(False)
            return result
        if action == "password":
            error = lock.set_password(str(body.get("new") or ""), str(body.get("old") or ""))
            return {"ok": not error, "error": error}
        raise HTTPException(status_code=404, detail="acao desconhecida")

    return router
