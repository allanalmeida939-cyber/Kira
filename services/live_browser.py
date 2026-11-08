"""Navegador da Kira ao vivo: Chromium sem janela (nada na barra de tarefas) que a Kira controla, com a imagem da
página transmitida para a aba Internet (screencast do Chromium: um quadro só quando a tela muda).

Roda numa thread própria, com o próprio laço de eventos: se o Chromium travar ou demorar, o backend e a conversa não
sentem. Sobe sob demanda e fecha sozinho depois de um tempo parado (ninguém vendo e nenhuma tarefa). O perfil fica
salvo (logins continuam). Só abre http/https; download só durante uma tarefa pedida pelo Chefe (vai para a pasta
Downloads); janela nova (pop-up) vira a página ativa.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import sys
import threading
import time
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("kira.app.live_browser")

VIEWPORT = {"width": 1280, "height": 800}
# Aba Internet nítida: a página é desenhada em 1,5x e mandada assim (texto definido mesmo esticado na tela)
SHARPNESS = 1.5
NAV_TIMEOUT_MS = 30000
ACTION_TIMEOUT_MS = 8000
CALL_TIMEOUT_S = 45.0
MAX_FPS = 8
SEARCH_URL = "https://www.google.com/search?hl=pt-BR&q="
SAFE_KEYS = frozenset({
    "Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "Home", "End", "PageUp", "PageDown",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Shift+Tab",
})
# Atalhos de vídeo usados pelos comandos de voz ("pausa", "tela cheia", "próximo"...)
MEDIA_KEYS = frozenset({"k", "f", "c", "m", "j", "l", "Shift+N", "Shift+P"})
RESULT_LINKS = (
    ("google.", "#search a:has(h3)"),
    ("youtube.com", "ytd-video-renderer a#video-title, ytd-rich-item-renderer a#video-title-link"),
    ("bing.com", "#b_results li.b_algo h2 a"),
    ("duckduckgo.com", "a[data-testid='result-title-a']"),
)
_DOMAIN = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]\S*)?$", re.IGNORECASE)

# Lista o que dá para clicar ou preencher (com um número em data-kira-id) e o texto que aparece na tela.
OBSERVE_JS = r"""
() => {
  const MAX = 150;
  const clean = (value, size = 80) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, size);
  document.querySelectorAll('[data-kira-id]').forEach((el) => el.removeAttribute('data-kira-id'));
  const selector = 'a[href], button, input:not([type=hidden]), textarea, select, summary, [contenteditable="true"], [contenteditable=""],'
    + ' [role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], [role=option], [role=switch], [role=combobox], [role=textbox]';
  const height = innerHeight;
  const labelOf = (el) => {
    const parts = [];
    const by = el.getAttribute('aria-labelledby');
    if (by) parts.push(by.split(/\s+/).map((id) => document.getElementById(id)?.innerText || '').join(' '));
    if (el.id) { const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (label) parts.push(label.innerText); }
    const wrap = el.closest('label'); if (wrap) parts.push(wrap.innerText);
    parts.push(el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('title'), el.getAttribute('alt'), el.getAttribute('name'));
    return clean(parts.find((part) => clean(part)) || '');
  };
  const found = [];
  for (const el of document.querySelectorAll(selector)) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) continue;
    found.push({ el, rect });
  }
  const distance = (rect) => (rect.bottom < 0 ? 100000 - rect.bottom : rect.top > height ? rect.top - height : 0);
  found.sort((a, b) => distance(a.rect) - distance(b.rect) || a.rect.top - b.rect.top || a.rect.left - b.rect.left);
  const elements = found.slice(0, MAX).map(({ el, rect }, index) => {
    const id = index + 1;
    el.setAttribute('data-kira-id', String(id));
    const tag = el.tagName.toLowerCase();
    const type = clean(el.getAttribute('type'), 20).toLowerCase();
    const item = { id, tag, onscreen: rect.bottom > 0 && rect.top < height };
    if (type) item.type = type;
    const role = el.getAttribute('role'); if (role) item.role = role;
    const text = clean(el.innerText || (tag === 'input' && ['submit', 'button'].includes(type) ? el.value : ''));
    if (text) item.text = text;
    const label = labelOf(el); if (label && label !== text) item.label = label;
    if (['input', 'textarea', 'select'].includes(tag)) {
      if (type === 'password') item.value = el.value ? '••••' : '';
      else if (['checkbox', 'radio'].includes(type)) item.checked = Boolean(el.checked);
      else if (tag === 'select') { item.value = clean(el.selectedOptions?.[0]?.text, 40); item.options = [...el.options].slice(0, 12).map((option) => clean(option.text, 40)); }
      else item.value = clean(el.value, 60);
      if (el.required) item.required = true;
    }
    if (tag === 'a') item.href = clean(el.getAttribute('href'), 60);
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') item.disabled = true;
    return item;
  });
  const chunks = [];
  let size = 0;
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node && size < 6000; node = walker.nextNode()) {
    const value = clean(node.nodeValue, 300);
    const parent = node.parentElement;
    if (!value || !parent || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(parent.tagName)) continue;
    const rect = parent.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > height * 1.5 || rect.width === 0) continue;
    chunks.push(value);
    size += value.length + 1;
  }
  return { url: location.href, title: document.title, elements, text: chunks.join(' ').slice(0, 6000),
           scroll: { y: Math.round(scrollY), height: document.documentElement.scrollHeight, view: height } };
}
"""


def normalize_target(raw: object) -> str | None:
    """O que abrir: URL http(s), domínio solto ("detran.ce.gov.br") ou, se não for endereço, uma busca no Google.
    None se vazio ou com esquema que não é de site (file:, javascript:, chrome:)."""
    text = " ".join(str(raw or "").split())[:500]
    if not text:
        return None
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme:
        if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
            return text
        if not _DOMAIN.match(text):
            return None
    if " " not in text and _DOMAIN.match(text):
        return "https://" + text
    return SEARCH_URL + urllib.parse.quote_plus(text)


class BrowserClosed(RuntimeError):
    """O Chromium fechou (ou caiu) no meio da ação: a próxima chamada abre de novo."""


class LiveBrowser:
    """Interface async para o backend; tudo do Playwright roda na thread do navegador."""

    def __init__(self, profile_dir: Path, *, idle_seconds: float = 300.0, headless: bool = True,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._profile = Path(profile_dir)
        self._idle_seconds = max(30.0, float(idle_seconds))
        self._headless = headless
        self._clock = clock
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread_lock = threading.Lock()
        # estado do lado do navegador (só a thread dele mexe; o resto só lê)
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None
        self._cdp: Any = None
        self._screencasting = False
        self._user_agent: str | None = None
        self._start_lock: asyncio.Lock | None = None
        self._watchdog: asyncio.Task | None = None
        self.frame: bytes | None = None
        self.frame_seq = 0
        self.viewers = 0
        self.busy = 0
        self.downloads: list[dict[str, str]] = []  # arquivos baixados nas tarefas (nome e pasta), os mais novos no fim
        self._last_used = clock()
        # tamanho da área da aba Internet: a página é desenhada nele, sem encolher a imagem (encolhida, o texto borrava)
        self._viewport = dict(VIEWPORT)
        self._background: dict[str, Any] = {}
        self._opening_background = False

    # ---------- thread do navegador ----------
    def _ensure_thread(self) -> asyncio.AbstractEventLoop:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive() and self._loop is not None:
                return self._loop
            ready = threading.Event()

            def runner() -> None:
                loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                self._start_lock = asyncio.Lock()
                ready.set()
                loop.run_forever()

            self._thread = threading.Thread(target=runner, name="kira-live-browser", daemon=True)
            self._thread.start()
            ready.wait(5.0)
            assert self._loop is not None
            return self._loop

    async def _call(self, factory: Callable[[], Awaitable[Any]], timeout: float = CALL_TIMEOUT_S) -> Any:
        """Roda `factory()` na thread do navegador e espera daqui, sem travar este laço."""
        loop = self._ensure_thread()
        future = asyncio.run_coroutine_threadsafe(factory(), loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        except asyncio.TimeoutError:
            future.cancel()
            raise

    def _touch(self) -> None:
        self._last_used = self._clock()

    # ---------- abrir, trocar de página, fechar (na thread do navegador) ----------
    async def _ensure(self) -> Any:
        assert self._start_lock is not None
        async with self._start_lock:
            if self._context is None:
                await self._launch()
            if self._page is None or self._page.is_closed():
                pages = [page for page in self._context.pages if not page.is_closed() and page not in self._background.values()]
                await self._activate(pages[-1] if pages else await self._context.new_page())
            self._touch()
            return self._page

    async def _launch(self) -> None:
        from playwright.async_api import async_playwright

        self._profile.mkdir(parents=True, exist_ok=True)
        started = self._clock()
        self._pw = await async_playwright().start()
        try:
            self._context = await self._pw.chromium.launch_persistent_context(
                str(self._profile), headless=self._headless, channel="chromium", viewport=VIEWPORT, locale="pt-BR", device_scale_factor=SHARPNESS,
                accept_downloads=True, args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            await self._pw.stop()
            self._pw = None
            raise
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)
        self._context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        # download em qualquer aba desde que ela nasce (link de download costuma abrir aba nova e baixar na hora)
        watch = lambda page: page.on("download", lambda download: asyncio.ensure_future(self._on_download(download)))  # noqa: E731
        for page in self._context.pages:
            watch(page)
        self._context.on("page", watch)
        self._context.on("page", self._on_new_page)
        self._context.on("close", lambda *_: self._forget("context_closed"))
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.ensure_future(self._idle_watch())
        logger.info("LIVE_BROWSER_STARTED ms=%d", int((self._clock() - started) * 1000))

    def _on_new_page(self, page: Any) -> None:
        if self._opening_background:
            return  # página de fundo (leituras do Instagram): não vira a página que a pessoa vê
        if page is not self._page:
            asyncio.ensure_future(self._activate(page))  # pop-up / aba nova: é para lá que a pessoa vai olhar

    async def _activate(self, page: Any) -> None:
        if page is self._page:
            return  # já é a ativa (ou está sendo ativada agora)
        old = self._cdp
        self._page, self._cdp, self._screencasting = page, None, False
        page.on("close", lambda *_: asyncio.ensure_future(self._on_page_closed(page)))
        cdp = await self._context.new_cdp_session(page)
        cdp.on("Page.screencastFrame", lambda params: self._on_frame(cdp, params))
        self._cdp = cdp
        await self._fix_user_agent(page, cdp)
        try:
            await page.set_viewport_size(self._viewport)
        except Exception:
            pass
        if old is not None:
            try:
                await old.detach()
            except Exception:
                pass
        await self._sync_screencast()

    async def _on_download(self, download: Any) -> None:
        """Download só durante uma tarefa (o Chefe pediu); fora disso é cancelado. Vai para a pasta Downloads, sem sobrescrever."""
        if self.busy <= 0:
            await download.cancel()
            logger.info("LIVE_BROWSER_DOWNLOAD_BLOCKED reason=no_task")
            return
        from services.documents import places, safe_name, unique_path

        folder = places()["downloads"]
        name = safe_name(download.suggested_filename or "download")
        stem, dot, ext = name.rpartition(".")
        target = unique_path(folder, stem or name, f".{ext}" if dot else "")
        try:
            await download.save_as(str(target))
        except Exception as exc:  # noqa: BLE001 - site cortou o download
            logger.info("LIVE_BROWSER_DOWNLOAD_FAILED reason=%s", type(exc).__name__)
            return
        self.downloads = (self.downloads + [{"nome": target.name, "pasta": str(folder)}])[-20:]
        logger.info("LIVE_BROWSER_DOWNLOADED bytes=%s", target.stat().st_size)

    async def _on_page_closed(self, page: Any) -> None:
        if page is not self._page or self._context is None:
            return
        self._page, self._cdp, self._screencasting = None, None, False
        pages = [item for item in self._context.pages if not item.is_closed() and item not in self._background.values()]
        if pages:
            await self._activate(pages[-1])

    async def _fix_user_agent(self, page: Any, cdp: Any) -> None:
        """Sem janela o Chromium se apresenta como "HeadlessChrome" e alguns sites bloqueiam: vira Chrome comum."""
        try:
            if self._user_agent is None:
                self._user_agent = str(await page.evaluate("navigator.userAgent")).replace("HeadlessChrome", "Chrome")
            version = re.search(r"Chrome/([\d.]+)", self._user_agent)
            full = version.group(1) if version else "120.0.0.0"
            major = full.split(".")[0]
            brands = [{"brand": "Chromium", "version": major}, {"brand": "Google Chrome", "version": major}, {"brand": "Not.A/Brand", "version": "99"}]
            await cdp.send("Emulation.setUserAgentOverride", {
                "userAgent": self._user_agent, "acceptLanguage": "pt-BR,pt;q=0.9", "platform": "Win32",
                "userAgentMetadata": {"brands": brands, "fullVersion": full, "platform": "Windows", "platformVersion": "10.0.0",
                                      "architecture": "x86", "model": "", "mobile": False},
            })
        except Exception as exc:
            logger.debug("LIVE_BROWSER_UA_SKIPPED reason=%s", type(exc).__name__)

    def _forget(self, reason: str) -> None:
        if self._context is None:
            return
        logger.info("LIVE_BROWSER_GONE reason=%s", reason)
        self._context = self._page = self._cdp = None
        self._background = {}
        self._screencasting = False
        self.frame = None
        pw, self._pw = self._pw, None
        if pw is not None:
            asyncio.ensure_future(self._stop_driver(pw))

    @staticmethod
    async def _stop_driver(pw: Any) -> None:
        try:
            await pw.stop()
        except Exception:
            pass

    async def _close(self) -> None:
        context, pw = self._context, self._pw
        self._context = self._page = self._cdp = self._pw = None
        self._screencasting = False
        self.frame = None
        for closer in (context, pw):
            if closer is None:
                continue
            try:
                await (closer.close() if closer is context else closer.stop())
            except Exception:
                pass
        if context is not None:
            logger.info("LIVE_BROWSER_CLOSED")

    async def _idle_watch(self) -> None:
        while self._context is not None:
            await asyncio.sleep(15.0)
            if self._context is not None and self.viewers <= 0 and self.busy <= 0 and self._clock() - self._last_used > self._idle_seconds:
                logger.info("LIVE_BROWSER_IDLE_CLOSE idle_s=%d", int(self._clock() - self._last_used))
                await self._close()

    # ---------- imagem ao vivo ----------
    def _on_frame(self, cdp: Any, params: dict) -> None:
        if cdp is self._cdp:
            try:
                self.frame = base64.b64decode(params.get("data") or "")
                self.frame_seq += 1
            except (ValueError, TypeError):
                pass
        asyncio.ensure_future(self._ack(cdp, params.get("sessionId")))

    @staticmethod
    async def _ack(cdp: Any, session_id: object) -> None:
        # O Chromium só captura o próximo quadro depois desta confirmação: segurar até o intervalo de MAX_FPS limita a
        # captura na origem (página animando: de ~1,2 núcleo de CPU capturando tudo para o necessário).
        await asyncio.sleep(1.0 / MAX_FPS)
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass

    async def _sync_screencast(self) -> None:
        cdp = self._cdp
        want = self.viewers > 0 and cdp is not None
        try:
            if want and not self._screencasting:
                await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": int(self._viewport["width"] * SHARPNESS), "maxHeight": int(self._viewport["height"] * SHARPNESS)})
                self._screencasting = True
            elif not want and self._screencasting and cdp is not None:
                await cdp.send("Page.stopScreencast")
                self._screencasting = False
        except Exception as exc:
            logger.debug("LIVE_BROWSER_SCREENCAST_SKIPPED reason=%s", type(exc).__name__)

    async def frames(self) -> AsyncIterator[bytes]:
        """Quadros JPEG para quem está com a aba aberta (no máximo MAX_FPS por segundo; parado, repete a cada 5 s)."""
        self.viewers += 1
        self._touch()
        try:
            await self._call(self._sync_screencast, timeout=10.0)
        except Exception:
            pass
        last_seq, last_sent = -1, 0.0
        try:
            while True:
                if self.frame is not None and (self.frame_seq != last_seq or time.monotonic() - last_sent > 5.0):
                    last_seq, last_sent = self.frame_seq, time.monotonic()
                    yield self.frame
                    await asyncio.sleep(1.0 / MAX_FPS)
                else:
                    await asyncio.sleep(0.1)
        finally:
            self.viewers -= 1
            self._touch()
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(self._sync_screencast(), self._loop)

    # ---------- ações (chamadas pelo backend) ----------
    async def _run(self, action: Callable[[Any], Awaitable[Any]], timeout: float = CALL_TIMEOUT_S) -> Any:
        async def job() -> Any:
            page = await self._ensure()
            try:
                return await action(page)
            except Exception as exc:
                if "closed" in str(exc).lower() and (self._context is None or page.is_closed()):
                    raise BrowserClosed("o navegador fechou no meio da ação") from exc
                raise

        return await self._call(job, timeout)

    async def state(self) -> dict[str, Any]:
        if self._context is None or self._loop is None:
            return {"open": False, "url": "", "title": "", "viewers": self.viewers}

        async def read() -> dict[str, Any]:
            page = self._page
            if page is None or page.is_closed():
                return {"open": self._context is not None, "url": "", "title": ""}
            try:
                title = await asyncio.wait_for(page.title(), 2.0)
            except Exception:
                title = ""
            return {"open": True, "url": page.url, "title": title}

        try:
            data = await self._call(read, timeout=4.0)
        except Exception:
            data = {"open": self._context is not None, "url": "", "title": ""}
        return {**data, "viewers": self.viewers}

    async def open(self) -> dict[str, Any]:
        await self._run(lambda page: asyncio.sleep(0))
        return await self.state()

    async def navigate(self, target: object) -> dict[str, Any]:
        url = normalize_target(target)
        if url is None:
            raise ValueError("endereço inválido")

        async def go(page: Any) -> None:
            from playwright.async_api import TimeoutError as PlaywrightTimeout

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except PlaywrightTimeout:
                pass  # carregou em parte: a pessoa já vê e a Kira segue

        await self._run(go)
        logger.info("LIVE_BROWSER_NAVIGATE host=%s", urllib.parse.urlsplit(url).netloc[:60])
        return await self.state()

    async def history(self, step: str) -> dict[str, Any]:
        async def move(page: Any) -> None:
            if step == "back":
                await page.go_back(wait_until="domcontentloaded")
            elif step == "forward":
                await page.go_forward(wait_until="domcontentloaded")
            else:
                await page.reload(wait_until="domcontentloaded")

        try:
            await self._run(move)
        except Exception as exc:
            logger.debug("LIVE_BROWSER_HISTORY_SKIPPED step=%s reason=%s", step, type(exc).__name__)
        return await self.state()

    async def pointer(self, kind: str, x: float, y: float, delta: float = 0.0) -> None:
        """Clique ou rolagem da pessoa na imagem ao vivo (x e y de 0 a 1 sobre a página)."""
        px = max(0.0, min(1.0, float(x))) * self._viewport["width"]
        py = max(0.0, min(1.0, float(y))) * self._viewport["height"]

        async def act(page: Any) -> None:
            if kind == "click":
                await page.mouse.click(px, py)
            else:
                await page.mouse.move(px, py)
                await page.mouse.wheel(0, max(-2400.0, min(2400.0, float(delta))))

        await self._run(act, timeout=15.0)

    async def keyboard(self, *, key: str = "", text: str = "") -> None:
        if key and key not in SAFE_KEYS and key not in MEDIA_KEYS:
            raise ValueError("tecla não permitida")

        async def act(page: Any) -> None:
            if key:
                await page.keyboard.press(key)
            if text:
                await page.keyboard.type(str(text)[:500])

        await self._run(act, timeout=20.0)

    async def scroll(self, direction: str, amount: float = 650.0) -> None:
        dy = abs(float(amount)) * (-1 if direction == "up" else 1)
        await self.pointer("wheel", 0.5, 0.5, dy)

    async def click_result(self, n: int) -> bool:
        """"Clica no segundo": o N-ésimo resultado (Google, YouTube, Bing, DuckDuckGo) ou o N-ésimo link com texto."""
        async def act(page: Any) -> bool:
            host = urllib.parse.urlsplit(page.url).netloc.lower()
            selector = next((css for part, css in RESULT_LINKS if part in host), "")
            if selector:
                links = page.locator(selector)
                if await links.count() >= n:
                    await links.nth(n - 1).click()
                    return True
                return False
            picked = await page.evaluate(
                """(n) => { const links = [...document.querySelectorAll('a[href]')].filter((a) => { const r = a.getBoundingClientRect();
                  return r.width > 0 && r.height > 0 && (a.innerText || '').trim().length >= 12; });
                  const link = links[n - 1]; if (!link) return false; link.setAttribute('data-kira-pick', '1'); return true; }""", n)
            if not picked:
                return False
            await page.locator('[data-kira-pick="1"]').first.click()
            return True

        return bool(await self._run(act))

    async def observe(self) -> dict[str, Any]:
        """O que a Kira "vê" para decidir o próximo passo: elementos numerados e o texto da tela."""
        async def read(page: Any) -> dict[str, Any]:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            return await page.evaluate(OBSERVE_JS)

        return await self._run(read)

    async def act(self, kind: str, element: int, value: str = "") -> None:
        """Ação num elemento numerado pelo `observe` (clicar, preencher, escolher opção, marcar)."""
        async def run(page: Any) -> None:
            locator = page.locator(f'[data-kira-id="{int(element)}"]').first
            if kind == "click":
                await locator.click()
            elif kind == "fill":
                tag = await locator.evaluate("(el) => el.tagName.toLowerCase()")
                if tag == "select":
                    try:
                        await locator.select_option(label=value)
                    except Exception:
                        await locator.select_option(value=value)
                else:
                    await locator.fill(value)
            elif kind == "check":
                await locator.check()
            elif kind == "uncheck":
                await locator.uncheck()
            else:
                raise ValueError("ação desconhecida")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass

        await self._run(run)

    async def resize(self, width: float, height: float) -> dict[str, int]:
        """A aba Internet mudou de tamanho: a página passa a ter esse tamanho (imagem 1:1, nítida)."""
        size = {"width": int(max(480, min(1920, width))), "height": int(max(360, min(1200, height)))}
        if abs(size["width"] - self._viewport["width"]) < 8 and abs(size["height"] - self._viewport["height"]) < 8:
            return self._viewport
        self._viewport = size
        if self._context is None or self._loop is None:
            return size

        async def apply() -> None:
            page = self._page
            if page is not None and not page.is_closed():
                await page.set_viewport_size(size)
            if self._screencasting and self._cdp is not None:
                await self._cdp.send("Page.stopScreencast")
                self._screencasting = False
            await self._sync_screencast()

        try:
            await self._call(apply, 10.0)
        except Exception as exc:
            logger.debug("LIVE_BROWSER_RESIZE_SKIPPED reason=%s", type(exc).__name__)
        return size

    async def session_get(self, url: str, headers: dict[str, str] | None = None, *, hosts: tuple[str, ...] = ("instagram.com",)) -> tuple[int, Any]:
        """GET com os cookies do perfil (o login que a pessoa fez na aba Internet), sem mexer na página que ela vê.
        Só para os sites da lista; devolve (status, JSON ou None)."""
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not url.startswith("https://") or not any(host == item or host.endswith("." + item) for item in hosts):
            raise ValueError("site nao permitido")

        async def job() -> tuple[int, Any]:
            await self._ensure()
            cookies = {item["name"]: item["value"] for item in await self._context.cookies(f"https://{host}/")}
            extra = {"Referer": f"https://{host}/", "X-Requested-With": "XMLHttpRequest", **(headers or {})}
            if cookies.get("csrftoken"):
                extra.setdefault("X-CSRFToken", cookies["csrftoken"])
            # de dentro de uma página do próprio site (escondida): o pedido sai igual ao do site, com os cabeçalhos
            # do navegador; pelo request do Playwright o Instagram respondia 429 ("espera um pouco")
            page = self._background.get(host)
            if page is None or page.is_closed():
                self._opening_background = True
                try:
                    page = await self._context.new_page()
                finally:
                    self._opening_background = False
                self._background[host] = page
                await self._fix_user_agent(page, await self._context.new_cdp_session(page))  # sem "HeadlessChrome"
                await page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            result = await page.evaluate(
                """async ([url, headers]) => {
                    const r = await fetch(url, { headers, credentials: 'include', redirect: 'manual' });
                    let body = null; try { body = await r.json(); } catch (_) {}
                    return [r.status, body];
                }""",
                [url, extra],
            )
            return int(result[0]), result[1]

        return await self._call(job, 30.0)

    async def session_cookie(self, host: str, name: str) -> str:
        async def job() -> str:
            await self._ensure()
            return next((item["value"] for item in await self._context.cookies(f"https://{host}/") if item["name"] == name), "")

        return await self._call(job, 20.0)

    async def close(self) -> None:
        if self._loop is None:
            return
        try:
            await self._call(self._close, timeout=15.0)
        except Exception:
            pass

    async def shutdown(self) -> None:
        """Fim do backend: fecha o Chromium e para a thread do navegador."""
        loop = self._loop
        if loop is None:
            return
        await self.close()
        loop.call_soon_threadsafe(loop.stop)


__all__ = ["LiveBrowser", "BrowserClosed", "normalize_target", "VIEWPORT"]
