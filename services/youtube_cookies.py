"""Login do YouTube para o player da Kira.

Nesta rede o YouTube trata quem não está logado como robô: todo vídeo embutido dá erro 150/153. Os cookies
exportados do navegador do Chefe (o mesmo arquivo que o yt-dlp usa) entram no navegador da Kira ao abrir, e o
player do painel passa a tocar logado. Só cookies de youtube.com; valores nunca vão para o log.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
COOKIES_FILE = Path(os.getenv("KIRA_YTDLP_COOKIES") or (ROOT / "configs" / "secure" / "ytdlp_cookies.txt"))


def parse_netscape_cookies(text: str) -> list[dict]:
    """Formato cookies.txt (Netscape): domínio, subdomínios, caminho, seguro, expira, nome, valor."""
    cookies = []
    for raw in str(text or "").splitlines():
        http_only = raw.startswith("#HttpOnly_")
        line = raw[len("#HttpOnly_"):] if http_only else raw
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _subdomains, path, secure, expires, name, value = parts
        host = domain.lstrip(".").lower()
        if not (host == "youtube.com" or host.endswith(".youtube.com")) or not name:
            continue
        try:
            expiry = int(float(expires or 0))
        except ValueError:
            continue
        cookies.append({
            "domain": domain, "path": path or "/", "secure": secure.upper() == "TRUE", "expires": expiry,
            "name": name, "value": value, "http_only": http_only,
        })
    return cookies


def install_youtube_cookies(views, path: Path = COOKIES_FILE) -> int:
    """Põe os cookies no perfil de cada janela Qt. Chamar na thread da interface; devolve quantos entraram."""
    if not path.is_file():
        logger.info("YOUTUBE_COOKIES_ABSENT")
        return 0
    cookies = parse_netscape_cookies(path.read_text(encoding="utf-8", errors="replace"))
    from qtpy.QtCore import QByteArray, QDateTime, QUrl
    from qtpy.QtNetwork import QNetworkCookie

    origin = QUrl("https://www.youtube.com")
    for view in views:
        store = view.profile.cookieStore()
        for item in cookies:
            cookie = QNetworkCookie(QByteArray(item["name"].encode("utf-8")), QByteArray(item["value"].encode("utf-8")))
            cookie.setDomain(item["domain"])
            cookie.setPath(item["path"])
            cookie.setSecure(item["secure"])
            cookie.setHttpOnly(item["http_only"])
            if item["expires"] > 0:
                cookie.setExpirationDate(QDateTime.fromSecsSinceEpoch(item["expires"]))
            if item["secure"]:
                # o player é um iframe dentro da página local: sem SameSite=None o cookie não vai junto
                cookie.setSameSitePolicy(QNetworkCookie.SameSite.None_)
            store.setCookie(cookie, origin)
    logger.info("YOUTUBE_COOKIES_INSTALLED count=%d windows=%d", len(cookies), len(views))
    return len(cookies)


__all__ = ["COOKIES_FILE", "install_youtube_cookies", "parse_netscape_cookies"]
