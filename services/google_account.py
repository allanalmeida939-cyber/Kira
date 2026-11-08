"""Conta Google principal da Kira (login oficial): o YouTube do Chefe na aba Música · Vídeos.

Uma vez só: o Chefe cria um "ID do cliente OAuth" do tipo App para computador no Google Cloud, salva o JSON baixado em
configs/google_client.json e aperta "Conectar conta Google". O token fica em configs/google_token.json (os dois fora do
git). O YouTube não libera histórico nem a página inicial para apps: aqui vêm os canais seguidos e as curtidas.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from services.local_request import is_local_request

CONFIGS = Path(__file__).resolve().parent.parent / "configs"
CLIENT_FILE, TOKEN_FILE = CONFIGS / "google_client.json", CONFIGS / "google_token.json"
SCOPES = "openid email https://www.googleapis.com/auth/youtube.readonly"
YT = "https://www.googleapis.com/youtube/v3/"
CACHE_SECONDS = 3600
# Caminho B: Chrome com um perfil só da Kira, logado no YouTube uma vez; escondido, ele lê recomendações e histórico
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE = CONFIGS.parent / "runtime" / "youtube_profile"
FEED_JS = r"""(() => { const out = [], seen = new Set(); const txt = (t) => t?.simpleText || t?.runs?.map((r) => r.text).join('') || t?.content || '';
  (function walk(o) { if (!o || typeof o !== 'object' || out.length >= 16) return;
    const v = o.videoRenderer || o.gridVideoRenderer || o.compactVideoRenderer;
    if (v?.videoId && !seen.has(v.videoId)) { seen.add(v.videoId); out.push({ videoId: v.videoId, title: txt(v.title), channel: txt(v.ownerText || v.shortBylineText || v.longBylineText) }); }
    const l = o.lockupViewModel, m = l?.metadata?.lockupMetadataViewModel;
    if (l?.contentId && l.contentType === 'LOCKUP_CONTENT_TYPE_VIDEO' && !seen.has(l.contentId)) { seen.add(l.contentId);
      out.push({ videoId: l.contentId, title: m?.title?.content || '', channel: m?.metadata?.contentMetadataViewModel?.metadataRows?.[0]?.metadataParts?.[0]?.text?.content || '' }); }
    for (const k in o) walk(o[k]); })(window.ytInitialData);
  return { logged: Boolean(window.yt?.config_?.LOGGED_IN), items: out }; })()"""


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _client() -> dict[str, Any]:
    data = _read(CLIENT_FILE)
    return data.get("installed") or data.get("web") or {}


def create_google_router(port: int) -> APIRouter:
    router = APIRouter()
    redirect = f"http://127.0.0.1:{port}/api/google/callback"
    state = {"nonce": "", "cache": (0.0, None)}

    def local(request: Request) -> None:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    async def access_token() -> str | None:
        token, client = _read(TOKEN_FILE), _client()
        if token.get("access_token") and token.get("expires_at", 0) > time.time() + 60:
            return token["access_token"]
        if not token.get("refresh_token") or not client:
            return None
        async with httpx.AsyncClient(timeout=15) as http:
            fresh = (await http.post("https://oauth2.googleapis.com/token", data={
                "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
                "refresh_token": token["refresh_token"], "grant_type": "refresh_token"})).json()
        if "access_token" not in fresh:
            return None
        token.update(access_token=fresh["access_token"], expires_at=time.time() + int(fresh.get("expires_in", 3600)))
        TOKEN_FILE.write_text(json.dumps(token), encoding="utf-8")
        return token["access_token"]

    @router.get("/api/google/status")
    async def status(request: Request) -> dict[str, Any]:
        local(request)
        return {"client": bool(_client()), "connected": bool(_read(TOKEN_FILE).get("refresh_token")), "email": _read(TOKEN_FILE).get("email", "")}

    @router.post("/api/google/connect")
    async def connect(request: Request) -> dict[str, Any]:
        local(request)
        client = _client()
        if not client:
            return {"ok": False, "message": "Falta o arquivo configs/google_client.json (ID do cliente do Google Cloud)."}
        state["nonce"] = secrets.token_urlsafe(16)
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
            "access_type": "offline", "prompt": "consent", "state": state["nonce"]})
        webbrowser.open(url, new=2)
        return {"ok": True}

    @router.get("/api/google/callback", response_class=HTMLResponse)
    async def callback(request: Request, code: str = "", error: str = "") -> str:
        local(request)
        if error or not code or request.query_params.get("state") != state["nonce"]:
            return "<h2>Não conectou. Volte na Kira e tente de novo.</h2>"
        client = _client()
        async with httpx.AsyncClient(timeout=15) as http:
            token = (await http.post("https://oauth2.googleapis.com/token", data={
                "client_id": client["client_id"], "client_secret": client.get("client_secret", ""), "code": code,
                "redirect_uri": redirect, "grant_type": "authorization_code"})).json()
            if "access_token" not in token:
                return "<h2>O Google recusou. Volte na Kira e tente de novo.</h2>"
            who = (await http.get("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": f"Bearer {token['access_token']}"})).json()
        token.update(expires_at=time.time() + int(token.get("expires_in", 3600)), email=who.get("email", ""))
        TOKEN_FILE.write_text(json.dumps(token), encoding="utf-8")
        state["cache"] = (0.0, None)
        return f"<h2>Conta {who.get('email', '')} conectada à Kira. Pode fechar esta aba.</h2>"

    @router.get("/api/youtube/mine")
    async def mine(request: Request) -> dict[str, Any]:
        """Fileiras da vitrine: vídeos novos dos canais seguidos e as curtidas (cota: ~12 unidades por hora)."""
        local(request)
        at, rows = state["cache"]
        if rows is not None and time.time() - at < CACHE_SECONDS:
            return {"ok": True, "rows": rows}
        token = await access_token()
        if not token:
            return {"ok": False, "rows": []}
        auth = {"Authorization": f"Bearer {token}"}
        card = lambda vid, sn: {"videoId": vid, "title": sn.get("title", ""), "channel": sn.get("videoOwnerChannelTitle") or sn.get("channelTitle", ""),
                                "thumbnail": ((sn.get("thumbnails") or {}).get("medium") or {}).get("url", ""), "watchUrl": f"https://www.youtube.com/watch?v={vid}"}
        async with httpx.AsyncClient(timeout=15, headers=auth) as http:
            subs = (await http.get(YT + "subscriptions", params={"part": "snippet", "mine": "true", "maxResults": 20, "order": "relevance"})).json().get("items", [])
            channels = [{"channelId": s_["snippet"]["resourceId"]["channelId"], "title": s_["snippet"].get("title", ""),
                         "thumbnail": ((s_["snippet"].get("thumbnails") or {}).get("medium") or {}).get("url", "")} for s_ in subs]
            subs = subs[:10]
            ids = ",".join(s["snippet"]["resourceId"]["channelId"] for s in subs)
            uploads = [c["contentDetails"]["relatedPlaylists"]["uploads"] for c in (await http.get(YT + "channels", params={"part": "contentDetails", "id": ids})).json().get("items", [])] if ids else []
            fresh = []
            for playlist in uploads[:10]:
                items = (await http.get(YT + "playlistItems", params={"part": "snippet", "playlistId": playlist, "maxResults": 2})).json().get("items", [])
                fresh += [(i["snippet"].get("publishedAt", ""), card(i["snippet"]["resourceId"]["videoId"], i["snippet"])) for i in items]
            liked = (await http.get(YT + "videos", params={"part": "snippet", "myRating": "like", "maxResults": 12})).json().get("items", [])
        rows = [{"title": "Canais que você segue", "channels": channels}] if channels else []
        rows += [r for r in (
            {"title": "Novos dos canais que você segue", "video": True, "items": [c for _, c in sorted(fresh, key=lambda x: x[0], reverse=True)][:12]},
            {"title": "Suas curtidas no YouTube", "video": True, "items": [card(v["id"], v["snippet"]) for v in liked]},
        ) if r["items"]]
        state["cache"] = (time.time(), rows)
        return {"ok": True, "rows": rows}

    @router.post("/api/youtube/browser-login")
    async def browser_login(request: Request) -> dict[str, Any]:
        local(request)
        subprocess.Popen([CHROME, f"--user-data-dir={PROFILE}", "--no-first-run",
                          "https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/"])
        state["feed"] = (0.0, None)
        return {"ok": True}

    async def scrape() -> dict[str, Any]:
        """Chrome escondido no perfil da Kira: lê a página inicial e o histórico do YouTube pelo DevTools e fecha."""
        port = 9334
        chrome = subprocess.Popen([CHROME, f"--user-data-dir={PROFILE}", "--headless=new", f"--remote-debugging-port={port}", "--no-first-run",
                                   "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36", "about:blank"])
        try:
            async with httpx.AsyncClient(timeout=3) as http:
                for _ in range(40):
                    try:
                        pages = (await http.get(f"http://127.0.0.1:{port}/json")).json()
                        break
                    except Exception:
                        await asyncio.sleep(0.25)
                else:
                    return {"logged": False, "rows": [], "error": "perfil em uso (feche o Chrome do YouTube da Kira)"}
            import aiohttp

            async with aiohttp.ClientSession() as session, session.ws_connect(next(p for p in pages if p["type"] == "page")["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                calls = iter(range(1, 99))

                async def cdp(method: str, **params: Any) -> Any:
                    ident = next(calls)
                    await ws.send_json({"id": ident, "method": method, "params": params})
                    async for msg in ws:
                        data = msg.json()
                        if data.get("id") == ident:
                            return data.get("result", {})

                rows, logged = [], False
                for title, url in (("Recomendados pra você no YouTube", "https://www.youtube.com/"), ("Do seu histórico", "https://www.youtube.com/feed/history")):
                    await cdp("Page.navigate", url=url)
                    await asyncio.sleep(6)
                    got = (await cdp("Runtime.evaluate", expression=FEED_JS, returnByValue=True)).get("result", {}).get("value") or {}
                    logged = logged or bool(got.get("logged"))
                    items = [{**i, "thumbnail": f"https://i.ytimg.com/vi/{i['videoId']}/mqdefault.jpg", "watchUrl": f"https://www.youtube.com/watch?v={i['videoId']}"} for i in got.get("items", [])]
                    if items:
                        rows.append({"title": title, "items": items, "video": True})
                return {"logged": logged, "rows": rows if logged else []}
        finally:
            chrome.kill()

    feed_file = CONFIGS.parent / "runtime" / "youtube_feed.json"  # último resultado: aparece na hora, até depois de reiniciar

    async def refresh_feed() -> None:
        try:
            data = await scrape()
        finally:
            state.pop("refreshing", None)
        if not data.get("logged"):  # sem login: não abre o Chrome escondido de novo por 10 min
            state["feed"] = (time.time() - CACHE_SECONDS + 600, data)
        else:
            state["feed"] = (time.time(), data)
            feed_file.write_text(json.dumps({"at": time.time(), "data": data}, ensure_ascii=False), encoding="utf-8")

    @router.get("/api/youtube/feed")
    async def feed(request: Request) -> dict[str, Any]:
        """Responde na hora com o último resultado e atualiza por trás quando passou de 1 h (o Chrome leva ~15 s)."""
        local(request)
        if "feed" not in state:
            saved = _read(feed_file)
            if saved.get("data"):
                state["feed"] = (float(saved.get("at") or 0), saved["data"])
        at, data = state.get("feed") or (0.0, None)
        stale = data is None or time.time() - at > CACHE_SECONDS
        if stale and Path(CHROME).exists() and not state.get("refreshing"):
            state["refreshing"] = asyncio.ensure_future(refresh_feed())
            if data is None:  # primeira vez na vida: espera a busca
                await state["refreshing"]
                at, data = state.get("feed") or (0.0, None)
        return data or {"logged": False, "rows": [], **({} if Path(CHROME).exists() else {"error": "Chrome não instalado"})}

    return router


__all__ = ["create_google_router"]
