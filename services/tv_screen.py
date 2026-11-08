"""Tela da Kira na TV LG (2026-11-06): a TV vira uma tela extra da Kira, aberta no navegador da TV.

O webOS não deixa trocar o menu/sistema; o que dá é abrir o navegador da TV numa página nossa. Ela é servida por um
servidor mínimo só na rede local (porta 3150), sem token e sem nada da API da Kira:
  GET /            -> interface/tv/tv.html (ou a página simples abaixo, se ainda não existir)
  GET /static/<n>  -> só a lista branca: interface/tv/* e o orbe (orbRenderersV2.js)
  GET /events      -> SSE com estado/voz, painéis, música e modelo ({"t": ...})
  GET /img?u=     -> imagem para a TV (a TV não alcança 127.0.0.1 nem o proxy /api/ui/img da Kira): só image/*,
                      até 8 MB, https público ou caminho /api/ do backend local; nunca IP privado de fora
  GET /alive?id=   -> 204; a página chama a cada 3 s (viva = último alive há menos de 8 s)
Ele sobe ao ativar e desce ao restaurar.
"""

from __future__ import annotations

import html
import ipaddress
import json
import queue
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from services.channel_logs import preview, tv_log
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import urlopen, HTTPRedirectHandler, Request, build_opener

PORT = 3150
TEMAS = ("orbe", "minimo", "neon")
ROOT = Path(__file__).resolve().parent.parent / "interface"
TV_DIR = ROOT / "tv"
ORB_JS = ROOT / "assets" / "js" / "ui" / "appearance" / "orbRenderersV2.js"
ALIVE_SECONDS = 8.0
BACKEND = "http://127.0.0.1:3000"  # a Kira (só no próprio PC)
IMG_MAX = 8 * 1024 * 1024
TYPES = {".webp": "image/webp", ".jpeg": "image/jpeg", ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".woff2": "font/woff2", ".json": "application/json", ".mp4": "video/mp4"}

PAGE = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Kira</title>
<style>html,body{margin:0;height:100%;overflow:hidden;background:#05030c;color:#efe7ff;font-family:Segoe UI,Arial,sans-serif}
.c{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
#h{font-size:16vh;font-weight:200}</style></head><body class="__TEMA__"><div class="c"><div id="h"></div></div>
<script>function t(){document.getElementById('h').textContent=new Date().toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit'})}
t();setInterval(t,15000);setInterval(function(){var x=new XMLHttpRequest();x.open('GET','/alive?id=simples');x.send()},3000);</script>
</body></html>"""


def page(tema: str) -> str:
    return PAGE.replace("__TEMA__", html.escape(tema if tema in TEMAS else TEMAS[0]))


def lan_ip(towards: str = "8.8.8.8") -> str:
    """IP deste PC na rede que alcança a TV (só abre o socket, não manda nada)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((towards, 9))
        return s.getsockname()[0]


# Olho de Deus na TV (2026-11-06): só estas camadas, só GET, só lat/lon/raio numéricos. A TV não alcança 127.0.0.1.
GOD_LAYERS = {"terremotos": "/api/godseye/earthquakes", "avioes": "/api/godseye/planes",
              "eventos": "/api/godseye/eonet", "raios": "/api/godseye/lightning"}
EARTH_JPG = Path(__file__).resolve().parent.parent / "interface" / "assets" / "ui" / "earth-night.jpg"


def fetch_layer(name: str, query: dict) -> bytes | None:
    """Repassa a camada do Olho de Deus do backend local (JSON), com coordenadas validadas."""
    path = GOD_LAYERS.get(name)
    if not path:
        return None
    args = []
    for key, lo, hi in (("lat", -90, 90), ("lon", -180, 180), ("radius_km", 1, 1000)):
        try:
            value = float((query.get(key) or [""])[0])
        except ValueError:
            continue
        if lo <= value <= hi:
            args.append(f"{key}={value:.4f}")
    if name == "avioes" and len(args) < 2:
        return None
    try:
        with urlopen(BACKEND + path + ("?" + "&".join(args) if args else ""), timeout=15) as r:
            return r.read(2 * 1024 * 1024)
    except (OSError, ValueError):
        return None


def static_file(name: str) -> Path | None:
    """Lista branca: arquivos direto em interface/tv/ (sem subir pasta), o orbe e o mapa da Terra."""
    if name == "orbRenderersV2.js":
        return ORB_JS
    if name == "earth-night.jpg":
        return EARTH_JPG
    candidate = (TV_DIR / name).resolve()
    if candidate.parent == TV_DIR.resolve() and candidate.is_file():
        return candidate
    return None


# TV = a MESMA Kira do PC (2026-11-06): a página da TV carrega os módulos de UI do PC (mesmos painéis, Olho de Deus,
# rota) por /assets/... e busca dados por /api/... — só estas pastas/extensões e só esta lista branca de /api.
ASSET_DIRS = ("assets/js/ui/", "assets/css/", "assets/ui/")
ASSET_EXT = {".js", ".css", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".json", ".woff2"}
# Lista branca de /api para a TV (sem WhatsApp, memória, configurações, tokens, educação pessoal):
GODS_USED = ("apod", "bikes", "earthquakes", "eonet", "keys", "lightning", "local", "neo", "planes", "radios", "rain",
             "satellites", "ships", "space", "suggest", "webcams")  # só o que o godsEye.js chama ("place" fica de fora)
API_GET = ("/api/ui/img", *(f"/api/godseye/{g}" for g in GODS_USED), "/api/tutor/quiz/", "/api/tutor/dados", "/api/design/file/", "/api/ui/creation/file/",
           "/api/ui/widgets/weather", "/api/ui/widgets/news", "/api/ui/widgets/scores", "/api/ui/widgets/file/", "/api/route/data/",
           "/api/vision/image/", "/api/music/state", "/api/casa/dispositivos")  # webcam (foto do cartão) e lista da Casa
API_POST = ("/api/tutor/card", "/api/tutor/responder", "/api/tutor/finalizar")  # clicar no simulado/flashcard na TV


def asset_file(path: str) -> Path | None:
    """/assets/... do PC, só nas pastas e extensões da lista branca (nunca sobe de pasta)."""
    rel = path.lstrip("/")
    if not rel.startswith(ASSET_DIRS) or Path(rel).suffix.lower() not in ASSET_EXT:
        return None
    candidate = (ROOT / rel).resolve()
    root = ROOT.resolve()
    return candidate if root in candidate.parents and candidate.is_file() else None


def api_allowed(path: str, method: str) -> bool:
    if ".." in path or "%2e" in path.lower():
        return False
    # prefixo exato: terminado em "/" exige algo depois; o resto é o caminho inteiro ou seguido de "/"
    match = lambda p: path.startswith(p) and len(path) > len(p) if p.endswith("/") else path == p or path.startswith(p + "/")  # noqa: E731
    return any(match(p) for p in (API_POST if method == "POST" else API_GET))


def _public(url: str) -> bool:
    """https e host que não resolve para rede privada/loopback (não vira proxy para dentro da casa)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # redireciona só por fetch_image, conferindo cada destino
        return None


def fetch_image(u: str) -> tuple[bytes, str] | None:
    """Caminho /api/... vai ao backend local; https público vai direto (até 3 redirecionamentos, todos públicos)."""
    if u.startswith("/api/") and not api_allowed(urlsplit(u).path, "GET"):
        return None  # /img não é atalho para fora da lista branca (fotos da webcam, WhatsApp...)
    url = BACKEND + u if u.startswith("/api/") else u
    opener = build_opener(_NoRedirect)
    for _ in range(4):
        if not (url.startswith(BACKEND + "/api/") or _public(url)):
            return None
        try:
            with opener.open(Request(url, headers={"User-Agent": "KiraAssistant/8.0 (tela da TV) python-urllib", "Accept": "image/*"}), timeout=8) as r:
                ctype = r.headers.get("Content-Type", "")
                data = r.read(IMG_MAX + 1)
        except Exception as exc:  # noqa: BLE001
            location = getattr(exc, "headers", None) and exc.headers.get("Location")
            if getattr(exc, "code", 0) in (301, 302, 303, 307, 308) and location:
                url = urljoin(url, location)
                continue
            return None
        if not ctype.startswith("image/") or len(data) > IMG_MAX:
            return None
        return data, ctype.split(";")[0]
    return None


class _Handler(BaseHTTPRequestHandler):
    screen: "TvScreenServer"

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_file(self, found: Path) -> None:
        """Arquivo estático com Range: o tocador da LG só aceita vídeo servido por pedaços (206); com 200 inteiro dava
        erro de decodificação (código 3), o vídeo do keep-awake nunca tocava e a TV entrava em descanso (2026-11-07)."""
        body, ctype = found.read_bytes(), TYPES.get(found.suffix.lower(), "application/octet-stream")
        rng = re.match(r"bytes=(\d+)-(\d*)$", self.headers.get("Range") or "")
        if rng and rng.group(2) and int(rng.group(2)) < int(rng.group(1)):  # intervalo invertido
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{len(body)}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if rng and int(rng.group(1)) < len(body):
            start = int(rng.group(1))
            end = min(int(rng.group(2) or len(body) - 1), len(body) - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            body = body[start:end + 1]
        else:
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # js/css/imagens (three.js e módulos do PC têm MBs): cache curto; o HTML sempre novo
        self.send_header("Cache-Control", "no-store" if found.suffix.lower() == ".html" else "max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def _trusted(self) -> bool:
        """Dados só para a página da Kira que está dando sinal (/alive) e com Host = este servidor (anti DNS rebinding)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host != self.server.server_address[0]:
            return False
        seen = self.screen.known_ips.get(self.client_address[0], 0.0)
        return time.monotonic() - seen < 120

    def do_GET(self) -> None:  # noqa: N802 - nome do http.server
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path.startswith(("/api/", "/img", "/gods/", "/rpc", "/audio-ok")) and not self._trusted():
            tv_log("TV_ACESSO_NEGADO", ip=self.client_address[0], path=preview(parts.path, 60))
            return self._send(403)
        if parts.path == "/":
            tv = TV_DIR / "tv.html"
            body = tv.read_bytes() if tv.is_file() else page((query.get("tema") or [""])[0]).encode("utf-8")
            return self._send(200, body, TYPES[".html"])
        if parts.path == "/alive":
            self.screen.mark_alive((query.get("id") or [""])[0])
            self.screen.known_ips[self.client_address[0]] = time.monotonic()
            return self._send(204)
        if parts.path.startswith("/static/"):
            found = static_file(parts.path[len("/static/"):])
            if found is None:
                return self._send(404)
            return self._send_file(found)
        if parts.path == "/events":
            tv_log("TV_EVENTS_CONECTOU", ip=self.client_address[0], ua=preview(self.headers.get("User-Agent"), 120))
            return self._events()
        if parts.path.startswith("/gods/"):
            got = fetch_layer(parts.path[len("/gods/"):], query)
            return self._send(200, got, "application/json") if got else self._send(404)
        if parts.path == "/img":
            got = fetch_image((query.get("u") or [""])[0][:2000])
            tv_log("TV_IMG", ok=bool(got), bytes=len(got[0]) if got else 0, url=preview((query.get("u") or [""])[0], 90))
            return self._send(200, got[0], got[1]) if got else self._send(404)
        if parts.path == "/audio-ok":  # a TV avisa que a faixa da Kira está tocando (ou bloqueada pelo autoplay)
            self.screen.audio_status = ((query.get("s") or [""])[0][:16], time.monotonic())
            tv_log("TV_AUDIO_STATUS", status=self.screen.audio_status[0])
            return self._send(204)
        if parts.path.startswith("/assets/"):
            found = asset_file(parts.path)
            if found is None:
                return self._send(404)
            return self._send_file(found)  # com cache curto: os módulos do PC e o three.js não descem a cada recarga
        if parts.path.startswith("/api/"):
            return self._proxy("GET", parts)
        if parts.path == "/rpc":  # resposta da TV a uma chamada (Olho de Deus, rota): {"t":"call"} -> /rpc?id=&r=<json>
            self.screen.resolve_call((query.get("id") or [""])[0][:40], (query.get("r") or ["null"])[0][:20000])
            return self._send(204)
        if parts.path == "/audio-token":  # voz da Kira na TV: token só-ouvinte, só com o modo ligado (204 = desligado)
            allowed = self.screen.audio_on and self.screen.audio_token and self.client_address[0] == self.screen.audio_ip
            if self.screen.audio_on and not allowed:
                tv_log("TV_AUDIO_TOKEN_DENIED", ip=self.client_address[0])
            made = self.screen.audio_token() if allowed else None
            return self._send(200, json.dumps(made).encode("utf-8"), "application/json") if made else self._send(204)
        return self._send(404)

    def do_POST(self) -> None:  # noqa: N802 - só a lista branca de /api (clicar em simulado/flashcard na TV)
        parts = urlsplit(self.path)
        if not parts.path.startswith("/api/"):
            return self._send(404)
        if not self._trusted():
            return self._send(403)
        return self._proxy("POST", parts)

    def _proxy(self, method: str, parts) -> None:
        """Repassa ao backend local só o que está na lista branca; resposta em pedaços (vale para a câmera ao vivo)."""
        if not api_allowed(parts.path, method):
            return self._send(404)
        body = None
        if method == "POST":
            try:
                size = max(0, min(int(self.headers.get("Content-Length") or 0), 64 * 1024))
            except ValueError:
                size = 0
            body = self.rfile.read(size) if size else b""
        url = BACKEND + parts.path + ("?" + parts.query if parts.query else "")
        try:
            r = urlopen(Request(url, data=body, method=method, headers={"Content-Type": self.headers.get("Content-Type") or "application/json"}), timeout=30)
        except Exception as exc:  # noqa: BLE001 - backend respondeu erro ou caiu
            return self._send(int(getattr(exc, "code", 502) or 502))
        with r:
            self.send_response(r.status)
            self.send_header("Content-Type", r.headers.get("Content-Type", "application/octet-stream"))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, ConnectionError):
                pass

    def _events(self) -> None:
        inbox = self.screen.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps(self.screen.hello())}\n\n".encode())
            self.wfile.flush()
            while self.screen.running:
                try:
                    msg = inbox.get(timeout=15)
                    chunk = f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    chunk = ": ping\n\n"  # keep-alive
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (OSError, ConnectionError):
            pass
        finally:
            self.screen.unsubscribe(inbox)
            tv_log("TV_EVENTS_DESCONECTOU", ip=self.client_address[0])

    def log_message(self, *args: object) -> None:  # silencioso
        return


class TvScreenServer:
    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._alive_at = 0.0
        self._last_state = 0.0
        self.modelo = 1
        self.state: dict = {"t": "state", "state": "idle", "level": 0.0}
        self.music: dict | None = None
        self.locked = False  # Kira trancada (kira_lock): a TV mostra a tela bloqueada
        self.running = False
        self.audio_on = False  # "fala pela TV" (kira_app /api/tv/audio)
        self.audio_token = None  # callable -> {"url","token"} só-ouvinte
        self.known_ips: dict[str, float] = {}  # IPs que deram sinal de vida (a TV); só eles leem /api e /img
        self.audio_ip = ""  # só esse IP (a TV) recebe o token de áudio
        self._calls: dict[str, dict] = {}  # chamadas de ida e volta pendentes (call/resolve_call)
        self.godseye = False  # o Olho de Deus está aberto NA TV (os comandos de foco vão para lá)
        self.audio_status: tuple[str, float] = ("", 0.0)  # ("playing"|"blocked", quando) vindo de /audio-ok
        self.principal = False  # "usa a TV como tela principal": música, abas casa/música e avisos vão para a TV
        # A tela da Kira saiu da TV (restaurou, ou a página sumiu falando pela TV): kira_app devolve a voz e o orbe ao PC
        self.on_gone = None  # callable() | None

    def _gone(self) -> None:
        self.principal = False
        callback = self.on_gone
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001 - nunca derruba o servidor da TV
                pass

    def _watch(self) -> None:
        """Voz na TV e a página parou de dar sinal (TV desligada/trocou de app) por 20 s: a voz volta ao PC."""
        while self.running:
            time.sleep(5)
            if self.audio_on and self._alive_at and time.monotonic() - self._alive_at > 20:
                tv_log("TV_SUMIU_VOZ_VOLTA_PC")
                self._gone()

    def start(self, host: str, port: int = PORT) -> int:
        if self._server is None:
            tv_log("TV_SERVIDOR_SUBIU", host=host, porta=port)
            handler = type("TvHandler", (_Handler,), {"screen": self})
            self._server = ThreadingHTTPServer((host, port), handler)
            self._server.daemon_threads = True
            self.running = True
            threading.Thread(target=self._server.serve_forever, daemon=True, name="kira-tv-screen").start()
            threading.Thread(target=self._watch, daemon=True, name="kira-tv-watch").start()
        return self._server.server_address[1]

    def stop(self) -> None:
        tv_log("TV_SERVIDOR_PAROU")
        self.running = False
        self._alive_at = 0.0
        self.godseye = False
        self._gone()  # restaurou a TV: voz, orbe e próximos painéis voltam ao PC
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ---------- presença ----------
    def mark_alive(self, _page_id: str = "") -> None:
        if not self.alive():
            tv_log("TV_VIVA", pagina=_page_id[:24])  # voltou a dar sinal (ou abriu agora)
        self._alive_at = time.monotonic()

    def alive(self) -> bool:
        return self._server is not None and time.monotonic() - self._alive_at < ALIVE_SECONDS

    # ---------- eventos ----------
    def subscribe(self) -> queue.Queue:
        inbox: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.append(inbox)
        return inbox

    def unsubscribe(self, inbox: queue.Queue) -> None:
        with self._lock:
            if inbox in self._subs:
                self._subs.remove(inbox)

    def call(self, target: str, method: str, args: object, timeout_s: float = 12.0) -> object:
        """Bloqueante (rode com asyncio.to_thread): manda {"t":"call"} e espera a TV responder em /rpc."""
        import secrets
        call_id = secrets.token_hex(8)
        waiter = {"event": threading.Event(), "result": None}
        self._calls[call_id] = waiter
        try:
            if not self.broadcast({"t": "call", "id": call_id, "target": target, "method": method, "args": args}):
                return None
            waiter["event"].wait(timeout_s)
            return waiter["result"]
        finally:
            self._calls.pop(call_id, None)

    def resolve_call(self, call_id: str, raw: str) -> None:
        waiter = self._calls.get(call_id)
        if waiter is None:
            return
        try:
            waiter["result"] = json.loads(raw)
        except ValueError:
            waiter["result"] = None
        waiter["event"].set()

    def hello(self) -> dict:
        return {"t": "hello", "modelo": self.modelo, "state": self.state.get("state"), "music": self.music, "locked": self.locked,
                "audio": bool(getattr(self, "audio_on", False))}

    def broadcast(self, msg: dict) -> int:
        """Thread-safe. Guarda estado/música/modelo para quem conectar depois. Devolve quantas TVs receberam."""
        kind = msg.get("t")
        if kind == "state":
            now = time.monotonic()
            same = msg.get("state") == self.state.get("state")
            if same and now - self._last_state < 0.1:  # no máx. ~10/s
                return 0
            self._last_state = now
            self.state = msg
        elif kind == "music":
            self.music = msg
        elif kind == "lock":
            self.locked = bool(msg.get("on"))
        elif kind == "modelo":
            try:
                self.modelo = int(msg.get("n") or 1)
            except (TypeError, ValueError):
                self.modelo = 1
        with self._lock:
            subs = list(self._subs)
        if kind != "state":  # estado/voz chega 10x/s: não entra no log
            action = msg.get("action") if isinstance(msg.get("action"), dict) else {}
            tv_log("TV_ENVIO", t=kind, tipo=action.get("type") or "", kind=action.get("kind") or "", id=action.get("id") or action.get("presentationId") or "", telas=len(subs))
        for inbox in subs:
            try:
                inbox.put_nowait(msg)
            except queue.Full:  # TV travada: descarta o mais velho
                try:
                    inbox.get_nowait()
                    inbox.put_nowait(msg)
                except (queue.Empty, queue.Full):
                    pass
        return len(subs)


SCREEN = TvScreenServer()
