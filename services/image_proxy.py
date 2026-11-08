"""Proxy local de imagens: o front so carrega imagens de 127.0.0.1, o backend valida o resto."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import socket
from pathlib import Path
from typing import Awaitable, Callable, Iterable
from urllib.parse import quote, urljoin, urlsplit

import httpx

from services.shared_http import ensure_tls

PROXY_PATH = "/api/ui/img?u="
MAX_IMAGE_BYTES = 3 * 1024 * 1024
CACHE_LIMIT_BYTES = 200 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 6.0
MAX_REDIRECTS = 3
THUMBNAIL_WIDTHS = frozenset({160, 320, 480})
CONTENT_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
EXTENSIONS = {ext: ctype for ctype, ext in CONTENT_TYPES.items()}
RESOLVER = Callable[[str], Awaitable[Iterable[str]]]


class ProxyError(Exception):
    """Imagem recusada; `code` e seguro para logar (sem URL completa)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def proxied_url(url: object) -> str:
    """Devolve a URL local do proxy para uma imagem remota (ou string vazia se invalida)."""
    candidate = str(url or "").strip()
    if candidate.startswith(PROXY_PATH):
        return candidate
    if not candidate.lower().startswith("https://") or len(candidate) > 1800:
        return ""
    return f"{PROXY_PATH}{quote(candidate, safe='')}"


def sniff_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


async def _system_resolver(host: str) -> Iterable[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%")[0]).is_global
    except ValueError:
        return False


def allowed_hosts_from_env(environment: dict[str, str] | None = None) -> frozenset[str]:
    """Lista de hosts do proxy (KIRA_IMG_PROXY_HOSTS, separada por virgula); vazia = qualquer host publico."""
    source = environment if environment is not None else os.environ
    return frozenset(h.strip().lower() for h in str(source.get("KIRA_IMG_PROXY_HOSTS") or "").split(",") if h.strip())


async def assert_public_https(url: str, resolver: RESOLVER, allowed_hosts: frozenset[str] = frozenset()) -> None:
    """Garante https sem credenciais, porta 443 e destino publico (guarda SSRF); levanta ProxyError."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ProxyError("url_invalida") from exc
    host = str(parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or port not in (None, 443):
        raise ProxyError("url_invalida")
    if allowed_hosts and not any(host == h or host.endswith(f".{h}") for h in allowed_hosts):
        raise ProxyError("host_nao_permitido")
    try:
        literal = [str(ipaddress.ip_address(host))]
    except ValueError:
        literal = []
    try:
        addresses = literal or list(await asyncio.wait_for(resolver(host), 4.0))
    except (OSError, asyncio.TimeoutError) as exc:
        raise ProxyError("dns_falhou") from exc
    if not addresses or not all(_is_public_ip(a) for a in addresses):
        raise ProxyError("destino_privado")


class ImageProxy:
    def __init__(
        self,
        cache_dir: Path,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: RESOLVER = _system_resolver,
        allowed_hosts: frozenset[str] = frozenset(),
        max_bytes: int = MAX_IMAGE_BYTES,
        cache_limit: int = CACHE_LIMIT_BYTES,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._transport = transport
        self._resolver = resolver
        self._allowed_hosts = allowed_hosts
        self._max_bytes = max_bytes
        self._cache_limit = cache_limit
        self._semaphore = asyncio.Semaphore(4)

    # ---------- validacao ----------
    async def _validate(self, url: str) -> None:
        await assert_public_https(url, self._resolver, self._allowed_hosts)

    # ---------- cache ----------
    def _cache_key(self, url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> tuple[bytes, str] | None:
        for ext, ctype in EXTENSIONS.items():
            path = self._cache_dir / f"{key}.{ext}"
            try:
                data = path.read_bytes()
                os.utime(path)
            except OSError:
                continue
            return data, ctype
        return None

    def _cache_put(self, key: str, data: bytes, ctype: str) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            (self._cache_dir / f"{key}.{CONTENT_TYPES[ctype]}").write_bytes(data)
            files = sorted((p for p in self._cache_dir.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in files)
            while files and total > self._cache_limit:
                oldest = files.pop(0)
                total -= oldest.stat().st_size
                oldest.unlink(missing_ok=True)
        except OSError:
            return

    async def thumbnail(self, url: str, width: int) -> tuple[bytes, str]:
        """Miniatura WebP de até `width` px (cartões de notícia): arquivos de 300-500 KB viram ~20-40 KB."""
        url = str(url or "").strip()
        key = f"{self._cache_key(url)}_w{width}"
        cached = await asyncio.to_thread(self._cache_get, key)
        if cached:
            return cached
        data, ctype = await self.fetch(url)
        if ctype == "image/gif":
            return data, ctype  # pode ser animado: entrega como veio
        small = await asyncio.to_thread(_shrink, data, width)
        if not small:
            return data, ctype
        await asyncio.to_thread(self._cache_put, key, small, "image/webp")
        return small, "image/webp"

    # ---------- download ----------
    async def fetch(self, url: str) -> tuple[bytes, str]:
        url = str(url or "").strip()
        key = self._cache_key(url)
        cached = await asyncio.to_thread(self._cache_get, key)
        if cached:
            return cached
        async with self._semaphore:
            data, ctype = await self._download(url)
        await asyncio.to_thread(self._cache_put, key, data, ctype)
        return data, ctype

    async def _download(self, url: str) -> tuple[bytes, str]:
        contact = " ".join(str(os.environ.get("KIRA_HTTP_CONTACT") or "https://github.com/kira-local/kira").split())[:180]
        headers = {"User-Agent": f"KiraAssistant/8.0 ({contact}) python-httpx", "Accept": "image/png,image/jpeg,image/webp,image/gif"}
        verify = await ensure_tls()
        async with httpx.AsyncClient(timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS), headers=headers, transport=self._transport, verify=verify) as client:
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                await self._validate(current)
                try:
                    async with client.stream("GET", current, follow_redirects=False) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise ProxyError("redirect_invalido")
                            current = urljoin(current, location)
                            continue
                        if response.status_code != 200:
                            raise ProxyError("status_invalido")
                        declared = response.headers.get("content-type", "").split(";")[0].strip().lower()
                        if declared not in CONTENT_TYPES:
                            raise ProxyError("tipo_invalido")
                        length = response.headers.get("content-length")
                        if length and length.isdigit() and int(length) > self._max_bytes:
                            raise ProxyError("muito_grande")
                        buffer = bytearray()
                        async for chunk in response.aiter_bytes():
                            buffer.extend(chunk)
                            if len(buffer) > self._max_bytes:
                                raise ProxyError("muito_grande")
                except httpx.HTTPError as exc:
                    raise ProxyError("rede") from exc
                data = bytes(buffer)
                if sniff_type(data) != declared:
                    raise ProxyError("conteudo_invalido")
                return data, declared
        raise ProxyError("redirects_demais")


def _shrink(data: bytes, width: int) -> bytes:
    """Reduz para `width` px de largura (sem aumentar) em WebP; b"" se a imagem não abrir."""
    from io import BytesIO

    from PIL import Image

    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 40_000_000:
                return b""
            image.draft("RGB", (width, width))
            image = image.convert("RGBA" if image.mode in {"RGBA", "LA", "P"} else "RGB")
            if image.width > width:
                image = image.resize((width, max(1, round(image.height * width / image.width))), Image.LANCZOS)
            out = BytesIO()
            image.save(out, "WEBP", quality=80, method=4)
            return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        return b""


__all__ = ["ImageProxy", "ProxyError", "PROXY_PATH", "allowed_hosts_from_env", "assert_public_https", "proxied_url", "sniff_type"]
