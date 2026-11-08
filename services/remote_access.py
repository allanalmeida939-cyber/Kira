"""Acesso pelo celular: um portão com senha na frente da Kira.

O portão escuta numa porta própria (3100). Sem login só existe a tela de entrar; com login ele repassa tudo (páginas,
API e conexões em tempo real) para o backend do próprio PC (127.0.0.1:3000), que trata o pedido como local. Por isso
nada passa sem a sessão: o portão tira a chave interna, a origem e os cookies de quem chega, marca o pedido como
remoto (X-Kira-Remote) e as rotas que configuram este acesso (/api/remote) não passam por ele.

Perfis (até 3, como na Netflix): o do Chefe é o dono (a Kira inteira, voz e tudo); os outros são perfis comuns, cada um
com a sua senha, que entram numa tela própria (interface/guest.html) e só alcançam as rotas /api/guest: conversam com a
Kira por texto e pela voz do navegador, com memória separada (services/guest_chat.py), e não veem nada do Chefe.

Senha: só o hash (scrypt com sal) em configs/secure/remote_access.json (fora do git). Sessão: cookie assinado,
HttpOnly e SameSite=Lax (com Strict o celular pedia a senha toda vez que a Kira era aberta por atalho, notificação ou link), 30 dias; trocar a senha ou "sair de todos os aparelhos" derruba todas. Senha errada: 5
por endereço a cada 15 min; 20 no total por hora travam a entrada por uma hora.

Link fixo de qualquer lugar: Tailscale (o portão só escuta no PC e o Tailscale entrega com HTTPS, que o navegador do
celular exige para o microfone). Com "Link público" o Tailscale abre o mesmo endereço para a internet (Funnel): entra
pelos dados móveis sem o app do Tailscale no celular; a senha e os limites de tentativa continuam valendo. Em casa, no
mesmo Wi-Fi: "Rede de casa" (escuta também na rede local, sem HTTPS).
"""

from __future__ import annotations

from urllib.parse import urlsplit

import asyncio
import base64
import hashlib
import hmac
import gzip
import html
import io
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("kira.app.remote")

GATEWAY_PORT = 3100
BACKEND_URL = "http://127.0.0.1:3000"
COOKIE = "kira_remote"
SESSION_SECONDS = 30 * 24 * 3600
# o celular continua entrando enquanto é usado: a sessão é renovada quando já passou um dia (30 dias sem uso: senha de novo)
RENEW_AFTER = 24 * 3600
LAST_PROFILE_COOKIE = "kira_perfil"  # só o id do último perfil que entrou neste aparelho (já vem marcado na tela de entrar)
MIN_PASSWORD, MAX_PASSWORD = 8, 128
IP_FAILS, IP_WINDOW = 5, 900.0
GLOBAL_FAILS, GLOBAL_WINDOW, LOCKOUT_SECONDS = 20, 3600.0, 3600.0
MAX_BODY = 25 * 1024 * 1024
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers",
                        "transfer-encoding", "upgrade"})
# O que vem do celular e não pode chegar ao backend como se fosse do PC
DROP_REQUEST = HOP_BY_HOP | {"host", "cookie", "origin", "referer", "content-length", "x-kira-internal", "x-kira-remote",
                             "x-kira-profile", "x-kira-role",
                             "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "forwarded"}
DROP_RESPONSE = HOP_BY_HOP | {"content-length", "set-cookie"}
# App instalável pelo Chrome: manifesto, ícone e service worker (interface/sw.js) também na tela de entrar
APP_HEAD = ('<link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#12081f">'
            '<link rel="apple-touch-icon" href="/assets/ui/app/kira-192.png">'
            "<script>navigator.serviceWorker&&navigator.serviceWorker.register('/sw.js').catch(function(){});</script>")
REMOTE_MARK = "<script>window.KIRA_REMOTE=true;</script>" + APP_HEAD
OWNER_ID = "dono"
OWNER_NAME = os.getenv("KIRA_OWNER_NAME", "Chefe").strip() or "Chefe"
MAX_PROFILES = 3
# O que cada perfil comum pode usar (o Chefe liga e desliga em Configurações > Celular > Perfis)
GUEST_FEATURES = {"voz": "Voz da Kira", "clima": "Clima", "noticias": "Notícias e resumo", "cozinha": "Cozinha (receitas e timers)",
                  "musica": "Música", "lembretes": "Lembretes e avisos"}


def profile_features(profile: dict[str, Any] | None) -> dict[str, bool]:
    """Funções liberadas para o perfil: o dono tem tudo; perfil comum, o que o Chefe deixou (novo: tudo ligado)."""
    if not profile or profile.get("role") == "owner":
        return {key: True for key in GUEST_FEATURES}
    saved = profile.get("features") if isinstance(profile.get("features"), dict) else {}
    return {key: bool(saved.get(key, True)) for key in GUEST_FEATURES}
# perfil comum: só a própria tela, o app e as rotas dele (todo o resto é do Chefe)
GUEST_PATHS = ("/api/guest/", "/assets/")
GUEST_EXACT = ("/", "/guest", "/sw.js", "/manifest.webmanifest", "/api/phone/push/key", "/api/phone/push/subscribe")
# Celular pelos dados móveis: abrir a Kira baixava 18 MB (PNGs de 2 MB, nada comprimido nem guardado). O portão
# entrega imagem grande em WebP na largura de celular, texto com gzip, e deixa o celular guardar os arquivos da tela
# (confere se mudaram: 304). A tela do PC não passa por aqui e não muda.
IMAGE_SHRINK_BYTES = 120 * 1024
IMAGE_MAX_WIDTH = 1280
COMPRESSIBLE = ("text/html", "text/css", "text/plain", "application/javascript", "text/javascript", "application/json",
                "application/manifest+json", "image/svg+xml")
MAX_BUFFER = 8 * 1024 * 1024
_shrunk: dict[str, tuple[bytes, str]] = {}


def shrink_image(data: bytes, key: str) -> tuple[bytes, str] | None:
    """PNG/JPEG grande -> WebP (até 1280 px de largura, com transparência). None se não compensar."""
    if key in _shrunk:
        return _shrunk[key]
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        image.load()
        if image.width > IMAGE_MAX_WIDTH:
            image = image.resize((IMAGE_MAX_WIDTH, round(image.height * IMAGE_MAX_WIDTH / image.width)), Image.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
        out = io.BytesIO()
        image.save(out, "WEBP", quality=78, method=4)
        result = (out.getvalue(), "image/webp")
    except Exception:  # imagem estranha: vai como veio
        return None
    if len(result[0]) >= len(data):
        return None
    if len(_shrunk) > 200:
        _shrunk.clear()
    _shrunk[key] = result
    return result


# o Chrome busca isto sem o cookie da sessão (antes de entrar): nada pessoal, só o app
PUBLIC_PATHS = ("/sw.js", "/manifest.webmanifest", "/assets/ui/app/")
TAILSCALE_PATHS = (r"C:\Program Files\Tailscale\tailscale.exe", r"C:\Program Files (x86)\Tailscale\tailscale.exe")


# ---------- senha e sessão ----------
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        name, n, r, p, salt, digest = str(stored).split("$")
        if name != "scrypt":
            return False
        expected = _unb64(digest)
        candidate = hashlib.scrypt(str(password).encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(candidate, expected)
    except (ValueError, TypeError):
        return False


def make_session(secret: str, generation: int, now: float, profile: str = OWNER_ID, profile_gen: int = 1) -> str:
    payload = _b64(json.dumps({"g": generation, "e": int(now + SESSION_SECONDS), "n": secrets.token_hex(8), "p": profile,
                               "pg": int(profile_gen)}).encode())
    signature = hmac.new(bytes.fromhex(secret), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def session_data(token: str, secret: str, generation: int, now: float) -> dict[str, Any] | None:
    try:
        payload, signature = str(token or "").split(".", 1)
        expected = hmac.new(bytes.fromhex(secret), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(_unb64(payload))
        if int(data.get("g")) != int(generation) or float(data.get("e")) <= now:
            return None
        return {"profile": str(data.get("p") or OWNER_ID), "profile_gen": int(data.get("pg") or 1),  # sessão antiga: do dono
                "expires": float(data.get("e"))}
    except (ValueError, TypeError, AttributeError):
        return None


def check_session(token: str, secret: str, generation: int, now: float) -> bool:
    return session_data(token, secret, generation, now) is not None


class LoginGuard:
    """Senha errada: 5 por endereço a cada 15 min; 20 no total por hora travam a entrada por uma hora."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._by_ip: dict[str, deque] = {}
        self._all: deque = deque()
        self._locked_until = 0.0

    def wait_seconds(self, ip: str, known_device: bool = False) -> float:
        now = self._clock()
        # aparelho que já entrou antes (cookie de perfil) não cai no travamento geral: senão qualquer um na internet
        # trancava o Chefe fora com poucas senhas erradas (revisão de segurança 2026-11-07)
        if now < self._locked_until and not known_device:
            return self._locked_until - now
        fails = self._by_ip.get(ip) or deque()
        while fails and now - fails[0] > IP_WINDOW:
            fails.popleft()
        return IP_WINDOW - (now - fails[0]) if len(fails) >= IP_FAILS else 0.0

    def failed(self, ip: str) -> None:
        now = self._clock()
        self._by_ip.setdefault(ip, deque()).append(now)
        self._all.append(now)
        while self._all and now - self._all[0] > GLOBAL_WINDOW:
            self._all.popleft()
        if len(self._all) >= GLOBAL_FAILS:
            self._locked_until = now + LOCKOUT_SECONDS
            self._all.clear()
            logger.warning("REMOTE_LOGIN_LOCKED seconds=%d", int(LOCKOUT_SECONDS))

    def succeeded(self, ip: str) -> None:
        self._by_ip.pop(ip, None)


# ---------- configuração (configs/secure/remote_access.json) ----------
class RemoteSettings:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self._data is None:
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raw = {}
                data = {"enabled": bool(raw.get("enabled")), "lan": bool(raw.get("lan")), "public_link": bool(raw.get("public_link")),
                        "password_hash": str(raw.get("password_hash") or ""),
                        "secret": str(raw.get("secret") or ""), "generation": int(raw.get("generation") or 1),
                        "profiles": [item for item in raw.get("profiles") or [] if isinstance(item, dict)]}
                if len(data["secret"]) != 64:
                    data["secret"] = secrets.token_hex(32)
                    self._write(data)
                self._data = data
            return dict(self._data)

    def update(self, **changes: Any) -> dict[str, Any]:
        data = self.load()
        data.update(changes)
        with self._lock:
            self._write(data)
            self._data = data
        return dict(data)

    # ---------- perfis ----------
    def profiles(self) -> list[dict[str, Any]]:
        """Dono primeiro (a senha dele é a "senha" de sempre), depois os perfis comuns."""
        data = self.load()
        owner = {"id": OWNER_ID, "name": OWNER_NAME, "role": "owner", "password_hash": data["password_hash"], "gen": 1}
        guests = [dict(item) for item in data.get("profiles") or [] if isinstance(item, dict) and item.get("id") != OWNER_ID]
        return [owner] + guests

    def profile(self, profile_id: str) -> dict[str, Any] | None:
        return next((item for item in self.profiles() if item["id"] == profile_id), None)

    def add_profile(self, name: str, password_hash: str) -> dict[str, Any]:
        name = " ".join(str(name or "").split())[:24]
        guests = self.profiles()[1:]
        if not name:
            raise ValueError("dê um nome ao perfil")
        if len(guests) + 1 >= MAX_PROFILES:
            raise ValueError(f"no máximo {MAX_PROFILES} perfis")
        if any(item["name"].lower() == name.lower() for item in self.profiles()):
            raise ValueError("já existe um perfil com esse nome")
        profile = {"id": "p" + secrets.token_hex(3), "name": name, "role": "guest", "password_hash": password_hash, "gen": 1,
                   "created": int(time.time())}
        self.update(profiles=guests + [profile])
        return profile

    def update_profile(self, profile_id: str, **changes: Any) -> dict[str, Any]:
        guests = self.profiles()[1:]
        found = next((item for item in guests if item["id"] == profile_id), None)
        if found is None:
            raise ValueError("perfil não encontrado")
        found.update(changes)
        self.update(profiles=guests)
        return found

    def remove_profile(self, profile_id: str) -> bool:
        guests = self.profiles()[1:]
        kept = [item for item in guests if item["id"] != profile_id]
        if len(kept) == len(guests):
            return False
        self.update(profiles=kept)
        return True

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp, self._path)

    def public(self) -> dict[str, Any]:
        data = self.load()
        profiles = [{"id": item["id"], "name": item["name"], "role": item["role"]} for item in self.profiles()]
        return {"enabled": data["enabled"], "lan": data["lan"], "public_link": data["public_link"], "has_password": bool(data["password_hash"]),
                "profiles": profiles}


# ---------- tela de entrar ----------
def login_page(message: str = "", profiles: list[dict[str, Any]] | None = None, chosen: str = OWNER_ID) -> str:
    note = f'<p class="erro">{html.escape(message)}</p>' if message else ""
    profiles = profiles or [{"id": OWNER_ID, "name": OWNER_NAME}]
    if len(profiles) > 1:
        # cartões dos perfis (sem JavaScript: rádio estilizado)
        cards = "".join(
            f'<label class="perfil"><input type="radio" name="perfil" value="{html.escape(item["id"])}"'
            f'{" checked" if item["id"] == chosen else ""}><span class="avatar">{html.escape(item["name"][:1].upper())}</span>'
            f'<span class="nome">{html.escape(item["name"])}</span></label>' for item in profiles)
        picker = f'<p>Quem está usando?</p><div class="perfis">{cards}</div>'
    else:
        picker = f'<input type="hidden" name="perfil" value="{html.escape(profiles[0]["id"])}">'
    return f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>Kira</title>{APP_HEAD}
<style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at 50% 30%,#2a1050,#07050f 70%);
font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#ede9fe}}
form{{width:min(360px,100%);display:grid;gap:14px;padding:28px 22px;border-radius:20px;background:rgba(14,10,30,.92);
box-shadow:0 20px 60px rgba(0,0,0,.6);position:relative;overflow:hidden}}
form::before{{content:"";position:absolute;top:0;left:14px;right:14px;height:2px;background:linear-gradient(90deg,transparent,#a855f7 12%,#f0abfc 26%,rgba(168,85,247,.4) 72%,transparent)}}
.orb{{width:64px;height:64px;margin:0 auto 4px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#f5d0fe,#a855f7 45%,#3b0764 80%);
box-shadow:0 0 30px rgba(168,85,247,.55)}}
h1{{margin:0;text-align:center;font-size:1.3rem;letter-spacing:.18em}}p{{margin:0;text-align:center;font-size:.85rem;color:#c4b5fd}}
input{{width:100%;height:48px;padding:0 14px;border:0;border-radius:12px;background:rgba(255,255,255,.07);color:#fff;font-size:1rem;outline:none;
box-shadow:inset 0 -2px 0 rgba(168,85,247,.45)}}input:focus{{box-shadow:inset 0 -2px 0 #e879f9;background:rgba(255,255,255,.1)}}
button{{height:48px;border:0;border-radius:12px;background:linear-gradient(90deg,#7c3aed,#c026d3);color:#fff;font-size:1rem;font-weight:700}}
.erro{{color:#fecaca}}
.perfis{{display:flex;justify-content:center;gap:14px;flex-wrap:wrap}}.perfil{{display:grid;justify-items:center;gap:6px;cursor:pointer}}
.perfil input{{position:absolute;opacity:0;pointer-events:none}}.avatar{{width:64px;height:64px;display:grid;place-items:center;border-radius:16px;
font-size:1.6rem;font-weight:800;background:linear-gradient(135deg,#7c3aed,#c026d3);box-shadow:0 0 0 2px transparent;transition:box-shadow .2s,transform .2s}}
.perfil input:checked+.avatar{{box-shadow:0 0 0 3px #f0abfc,0 0 22px rgba(240,171,252,.45);transform:scale(1.06)}}.nome{{font-size:.85rem;color:#ddd6fe}}
</style></head><body><form method="post" action="/entrar" autocomplete="on">
<div class="orb" aria-hidden="true"></div><h1>KIRA</h1><p>Acesso pelo celular</p>{note}{picker}
<input type="password" name="senha" placeholder="Senha" autocomplete="current-password" required autofocus aria-label="Senha">
<button type="submit">Entrar</button></form></body></html>"""


# ---------- o portão ----------
def create_gateway_app(settings: RemoteSettings, *, backend_url: str = BACKEND_URL, clock: Callable[[], float] = time.time,
                       transport: Any = None, ws_connect: Callable[..., Any] | None = None):
    import httpx
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocket, WebSocketDisconnect

    guard = LoginGuard(clock)
    client = httpx.AsyncClient(base_url=backend_url, transport=transport, follow_redirects=False,
                               timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=10.0))
    ws_base = backend_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    security = {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}

    def signed_in(connection: Any) -> dict[str, Any] | None:
        """Perfil da sessão (dono ou comum) ou None."""
        data = settings.load()
        if not (data["enabled"] and data["password_hash"]):
            return None
        session = session_data(connection.cookies.get(COOKIE, ""), data["secret"], data["generation"], clock())
        profile = settings.profile(session["profile"]) if session else None
        if profile is None or int(profile.get("gen") or 1) != session["profile_gen"]:
            return None
        return profile

    def guest_allowed(path: str) -> bool:
        return path in GUEST_EXACT or (path.startswith(GUEST_PATHS) and ".." not in path)

    def blocked_path(path: str) -> bool:
        return path.rstrip("/").startswith("/api/remote")

    def public_path(path: str) -> bool:
        return path in PUBLIC_PATHS[:2] or (path.startswith(PUBLIC_PATHS[2]) and ".." not in path)

    async def entrar(request: Request) -> Response:
        if request.method == "GET":
            if signed_in(request):
                return RedirectResponse("/", status_code=303)
            last = str(request.cookies.get(LAST_PROFILE_COOKIE) or OWNER_ID)
            return HTMLResponse(login_page(profiles=settings.profiles(), chosen=last), headers={**security, "Cache-Control": "no-store"})
        ip = request.client.host if request.client else "?"
        if ip in {"127.0.0.1", "::1"}:  # atrás do Tailscale todos chegam como 127.0.0.1: usa o IP real repassado
            ip = (request.headers.get("x-forwarded-for") or ip).split(",")[0].strip() or ip
        wait = guard.wait_seconds(ip, known_device=bool(request.cookies.get(LAST_PROFILE_COOKIE)))
        if wait > 0:
            minutes = max(1, int(wait // 60) + 1)
            return HTMLResponse(login_page(f"Muitas tentativas. Tente de novo em {minutes} min.", settings.profiles()), status_code=429,
                                headers={**security, "Cache-Control": "no-store"})
        form = await request.form()
        password = str(form.get("senha") or "")[:MAX_PASSWORD]
        data = settings.load()
        profile = settings.profile(str(form.get("perfil") or OWNER_ID)) or settings.profile(OWNER_ID)
        ok = bool(data["enabled"] and data["password_hash"] and profile.get("password_hash")) and await asyncio.to_thread(
            verify_password, password, profile["password_hash"])
        if not ok:
            guard.failed(ip)
            logger.warning("REMOTE_LOGIN_FAILED")
            return HTMLResponse(login_page("Senha errada.", settings.profiles(), profile["id"]), status_code=401,
                                headers={**security, "Cache-Control": "no-store"})
        guard.succeeded(ip)
        logger.info("REMOTE_LOGIN_OK role=%s", profile["role"])
        response = RedirectResponse("/", status_code=303, headers=security)
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(COOKIE, make_session(data["secret"], data["generation"], clock(), profile["id"], int(profile.get("gen") or 1)),
                            max_age=SESSION_SECONDS,
                            httponly=True, samesite="lax", secure=secure, path="/")
        response.set_cookie(LAST_PROFILE_COOKIE, profile["id"], max_age=365 * 24 * 3600, httponly=True, samesite="lax", secure=secure, path="/")
        return response

    def renew(request: Request, response: Response, profile: dict[str, Any]) -> Response:
        """Sessão usada: renova os 30 dias (o celular fica entrando sem pedir a senha de novo)."""
        data = settings.load()
        session = session_data(request.cookies.get(COOKIE, ""), data["secret"], data["generation"], clock())
        if session and session["expires"] - clock() < SESSION_SECONDS - RENEW_AFTER:
            secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            response.set_cookie(COOKIE, make_session(data["secret"], data["generation"], clock(), profile["id"], int(profile.get("gen") or 1)),
                                max_age=SESSION_SECONDS, httponly=True, samesite="lax", secure=secure, path="/")
        return response

    async def sair(request: Request) -> Response:
        response = RedirectResponse("/entrar", status_code=303, headers=security)
        response.delete_cookie(COOKIE, path="/")
        return response

    async def proxy(request: Request) -> Response:
        path = request.url.path
        # anti-CSRF: pedido que muda algo vindo de OUTRO site é recusado (o portão apaga Origin antes do backend)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin") or ""
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin and urlsplit(origin).netloc != request.headers.get("host", "")):
                return JSONResponse({"error": "origem recusada"}, status_code=403, headers=security)
        if blocked_path(path):
            return JSONResponse({"error": "isso só pelo PC"}, status_code=403, headers=security)
        profile = signed_in(request)
        if profile is None and not (request.method == "GET" and public_path(path)):
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse("/entrar", status_code=303, headers=security)
            return JSONResponse({"error": "entre com a senha"}, status_code=401, headers=security)
        guest = profile is not None and profile["role"] != "owner"
        if guest and not guest_allowed(path):
            return JSONResponse({"error": "isso é só do dono da Kira"}, status_code=403, headers=security)
        if guest and path == "/":
            path = "/guest"  # perfil comum: a tela dele, não a do Chefe
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "arquivo grande demais"}, status_code=413, headers=security)
        headers = [(key, value) for key, value in request.headers.items() if key.lower() not in DROP_REQUEST]
        headers.append(("x-kira-remote", "1"))
        if profile is not None:
            headers += [("x-kira-profile", profile["id"]), ("x-kira-role", profile["role"])]
        target = path + (f"?{request.url.query}" if request.url.query else "")
        try:
            upstream = await client.send(client.build_request(request.method, target, headers=headers, content=body), stream=True)
        except httpx.HTTPError:
            return JSONResponse({"error": "a Kira do PC não respondeu"}, status_code=502, headers=security)
        answer = {key: value for key, value in upstream.headers.items() if key.lower() not in DROP_RESPONSE}
        answer.update(security)
        kind = upstream.headers.get("content-type", "")
        gzip_ok = "gzip" in request.headers.get("accept-encoding", "")
        if path.startswith("/assets/") and upstream.status_code in (200, 304):
            # arquivos da tela: o celular guarda e só confere se mudaram (o PC continua sem guardar nada)
            answer["cache-control"] = "no-cache"
        if kind.startswith("text/html"):
            # marca a página como aberta pelo celular (sem voz: a sessão de voz continua no PC)
            text = (await upstream.aread()).decode("utf-8", errors="replace")
            await upstream.aclose()
            answer.pop("content-encoding", None)
            if "<head>" in text:
                mark = REMOTE_MARK
                if profile is not None:
                    who = json.dumps({"id": profile["id"], "name": profile["name"], "role": profile["role"]}, ensure_ascii=False)
                    mark += "<script>window.KIRA_PROFILE=" + who.replace("<", "\\u003c") + ";</script>"
                text = text.replace("<head>", "<head>" + mark, 1)
            if gzip_ok:
                answer.update({"content-encoding": "gzip", "vary": "Accept-Encoding"})
                page = Response(gzip.compress(text.encode("utf-8"), 6), status_code=upstream.status_code, headers=answer,
                                media_type="text/html; charset=utf-8")
            else:
                page = HTMLResponse(text, status_code=upstream.status_code, headers=answer)
            return renew(request, page, profile) if profile is not None else page
        size = int(upstream.headers.get("content-length") or 0)
        image = kind.startswith(("image/png", "image/jpeg")) and size >= IMAGE_SHRINK_BYTES
        text_like = kind.startswith(COMPRESSIBLE) and gzip_ok and 1024 <= size <= MAX_BUFFER and not upstream.headers.get("content-encoding")
        if upstream.status_code == 200 and size <= MAX_BUFFER and (image or text_like):
            data = await upstream.aread()
            await upstream.aclose()
            if image:
                small = await asyncio.to_thread(shrink_image, data, f"{target}|{upstream.headers.get('etag', '')}|{size}")
                if small is not None:
                    data, kind = small
                    answer["content-type"] = kind
            elif text_like:
                data = await asyncio.to_thread(gzip.compress, data, 6)
                answer.update({"content-encoding": "gzip", "vary": "Accept-Encoding"})
            return Response(data, status_code=200, headers=answer)

        async def body_stream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(body_stream(), status_code=upstream.status_code, headers=answer)

    async def ws_proxy(websocket: WebSocket) -> None:
        path = websocket.url.path
        profile = signed_in(websocket)
        if blocked_path(path) or profile is None or profile["role"] != "owner":  # tempo real é só do dono
            await websocket.close(code=4401)
            return
        import websockets

        connect = ws_connect or websockets.connect
        target = ws_base + path + (f"?{websocket.url.query}" if websocket.url.query else "")
        try:
            # origem da própria tela do PC: a conexão dos painéis só aceita essa (o pedido já passou pelo login)
            upstream_cm = connect(target, origin=backend_url, open_timeout=5, max_size=None, additional_headers={"X-Kira-Remote": "1"})
            async with upstream_cm as upstream:
                await websocket.accept()

                async def from_phone() -> None:
                    try:
                        while True:
                            message = await websocket.receive()
                            if message["type"] == "websocket.disconnect":
                                return
                            if message.get("text") is not None:
                                await upstream.send(message["text"])
                            elif message.get("bytes") is not None:
                                await upstream.send(message["bytes"])
                    except WebSocketDisconnect:
                        return

                async def from_pc() -> None:
                    async for data in upstream:
                        if isinstance(data, str):
                            await websocket.send_text(data)
                        else:
                            await websocket.send_bytes(data)

                tasks = [asyncio.ensure_future(from_phone()), asyncio.ensure_future(from_pc())]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        except Exception as exc:
            logger.debug("REMOTE_WS_CLOSED reason=%s", type(exc).__name__)
        try:
            await websocket.close()
        except Exception:
            pass

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    app = Starlette(routes=[
        Route("/entrar", entrar, methods=["GET", "POST"]),
        Route("/sair", sair, methods=["GET", "POST"]),
        WebSocketRoute("/{path:path}", ws_proxy),
        Route("/{path:path}", proxy, methods=methods),
    ])
    app.state.client = client
    return app


# ---------- Tailscale (link fixo com HTTPS) e rede de casa ----------
def find_tailscale() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    return next((path for path in TAILSCALE_PATHS if Path(path).exists()), None)


def tailscale_state(run: Callable[..., Any] = subprocess.run, exe: str | None = None) -> dict[str, Any]:
    exe = exe or find_tailscale()
    if not exe:
        return {"installed": False, "running": False, "name": ""}
    try:
        done = run([exe, "status", "--json"], capture_output=True, text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(done.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"installed": True, "running": False, "name": ""}
    name = str(((data.get("Self") or {}).get("DNSName") or "")).rstrip(".")
    return {"installed": True, "running": data.get("BackendState") == "Running" and bool(name), "name": name}


FUNNEL_ENABLE_RE = re.compile(r"https://login\.tailscale\.com/\S+")


def tailscale_serve(on: bool, port: int = GATEWAY_PORT, run: Callable[..., Any] = subprocess.run, exe: str | None = None,
                    public: bool = False) -> str:
    """Liga (ou desliga) o HTTPS do Tailscale apontando para o portão: só para os aparelhos do Chefe (serve) ou aberto
    para a internet (funnel). Devolve "" se deu certo ou o motivo."""
    exe = exe or find_tailscale()
    if not exe:
        return "Tailscale não instalado"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    target = f"http://127.0.0.1:{port}"
    if not on:
        commands = [[exe, "funnel", "--https=443", "off"], [exe, "serve", "--https=443", "off"]]
    elif public:
        commands = [[exe, "funnel", "--bg", "--https=443", target]]
    else:
        commands = [[exe, "funnel", "--https=443", "off"], [exe, "serve", "--bg", "--https=443", target]]
    done = None
    for command in commands:
        try:
            done = run(command, capture_output=True, text=True, timeout=15, creationflags=flags)
        except subprocess.TimeoutExpired as exc:
            # Funnel nunca ligado nesta conta: o Tailscale espera o Chefe liberar num link e não volta sozinho
            link = FUNNEL_ENABLE_RE.search(str(exc.stdout or "") + str(exc.stderr or ""))
            return f"libere o Funnel uma vez neste link: {link.group(0)}" if link else "o Tailscale não respondeu"
        except (OSError, subprocess.SubprocessError) as exc:
            return type(exc).__name__
    if done is None or done.returncode == 0:
        return ""
    message = " ".join(str(done.stderr or done.stdout or "falhou").split())
    link = FUNNEL_ENABLE_RE.search(message)
    return f"libere o Funnel uma vez neste link: {link.group(0)}" if link and public else message[:200]


def lan_address() -> str:
    """IP do PC na rede de casa (sem mandar nada: o "connect" de UDP só escolhe a interface)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


class RemoteGateway:
    """Liga, desliga e troca o endereço do portão (thread própria com laço próprio, sem mexer no backend)."""

    def __init__(self, settings: RemoteSettings, *, port: int = GATEWAY_PORT, app_factory: Callable[..., Any] = create_gateway_app,
                 tailscale: Callable[[], dict[str, Any]] = tailscale_state, serve: Callable[..., str] = tailscale_serve) -> None:
        self._settings, self._port, self._factory = settings, port, app_factory
        self._tailscale, self._serve = tailscale, serve
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._host = ""
        self._error = ""
        self._serve_error = ""
        self._lock = threading.Lock()

    def apply(self) -> dict[str, Any]:
        data = self._settings.load()
        want = bool(data["enabled"] and data["password_hash"])
        host = "0.0.0.0" if data["lan"] else "127.0.0.1"
        with self._lock:
            if self._server is not None and (not want or host != self._host):
                self._stop()
            if want and self._server is None:
                self._start(host)
        state = self._tailscale()
        if state.get("running"):
            self._serve_error = self._serve(want, public=bool(data["public_link"]))
        return self.status(state)

    def watch(self, every_s: float = 60.0) -> None:
        """Vigia do link fixo: com o acesso ligado, a cada minuto confere o Tailscale e, se ele voltou (PC acordou,
        app foi reaberto, rede caiu e voltou) ou o HTTPS dele sumiu, liga de novo. Antes o link só era ligado ao abrir a
        Kira, e com o Tailscale fechado nessa hora o celular ficava sem link até mexer nas configurações (2026-10-31)."""
        if getattr(self, "_watching", False):
            return
        self._watching = True

        def loop() -> None:
            last = None
            while True:
                time.sleep(every_s)
                try:
                    data = self._settings.load()
                    if not (data["enabled"] and data["password_hash"]):
                        last = None
                        continue
                    state = self._tailscale()
                    up = bool(state.get("running"))
                    if up and (last is not True or self._serve_error or self._server is None):
                        self.apply()
                        logger.info("REMOTE_TAILSCALE_REAPPLIED name=%s error=%s", state.get("name", ""), self._serve_error or "-")
                    last = up
                except Exception as exc:  # a vigia nunca derruba nada
                    logger.info("REMOTE_TAILSCALE_WATCH_FAILED reason=%s", type(exc).__name__)

        threading.Thread(target=loop, name="kira-remote-watch", daemon=True).start()

    def _start(self, host: str) -> None:
        import uvicorn

        config = uvicorn.Config(self._factory(self._settings), host=host, port=self._port, log_level="warning", access_log=False, lifespan="off")
        server = uvicorn.Server(config)
        self._error = ""

        def run() -> None:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(server.serve())
            except (OSError, SystemExit) as exc:
                self._error = "a porta 3100 está ocupada" if isinstance(exc, (OSError, SystemExit)) else type(exc).__name__
            finally:
                loop.close()

        self._thread = threading.Thread(target=run, name="kira-remote-gateway", daemon=True)
        self._server, self._host = server, host
        self._thread.start()
        for _ in range(50):
            if server.started or not self._thread.is_alive():
                break
            time.sleep(0.05)
        if not server.started:
            self._error = self._error or "a porta 3100 está ocupada"
            self._server, self._thread = None, None
        logger.info("REMOTE_GATEWAY_STARTED host=%s port=%d ok=%s", host, self._port, str(server.started).lower())

    def _stop(self) -> None:
        server, thread = self._server, self._thread
        self._server, self._thread, self._host = None, None, ""
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=8.0)
        logger.info("REMOTE_GATEWAY_STOPPED")

    def stop(self) -> None:
        with self._lock:
            self._stop()

    def status(self, tailscale: dict[str, Any] | None = None) -> dict[str, Any]:
        state = tailscale if tailscale is not None else self._tailscale()
        running = self._server is not None
        links = {}
        if running and self._host == "0.0.0.0":
            address = lan_address()
            if address:
                links["casa"] = f"http://{address}:{self._port}"
        if running and state.get("running") and not self._serve_error:
            links["fixo"] = f"https://{state['name']}"
        public = self._settings.public()
        # Com "Rede de casa" marcada, o endereço aparece mesmo antes de o acesso ligar (o Chefe vê qual vai ser).
        address = lan_address() if public.get("lan") and "casa" not in links else ""
        return {**public, "running": running, "error": self._error, "links": links,
                "lan_preview": f"http://{address}:{self._port}" if address else links.get("casa", ""),
                "tailscale": {**state, "error": self._serve_error}}


__all__ = ["GUEST_FEATURES", "profile_features", "RemoteSettings", "RemoteGateway", "create_gateway_app", "hash_password", "verify_password", "make_session",
           "check_session", "LoginGuard", "tailscale_state", "tailscale_serve", "login_page", "COOKIE", "MIN_PASSWORD"]
