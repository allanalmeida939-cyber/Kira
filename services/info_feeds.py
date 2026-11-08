"""Informação sob pedido, além do get_news (que já cobre notícias por assunto e os feeds por tema):

- manchetes de um veículo pelo nome ("o que tem no Tecnoblog?"), por RSS (feedparser), com os feeds do Chefe em
  configs/rss_feeds.json somados aos padrão;
- ler uma matéria ("lê a segunda", "lê essa do Galaxy"): o texto limpo da página (trafilatura) vai para a Kira resumir;
- cotação de ação, índice, moeda ou cripto (yfinance), com o gráfico do último mês;
- o que o mundo está noticiando sobre um assunto (GDELT, em português primeiro), em várias fontes e países.

Texto de fora é dado, nunca instrução: volta para a Kira dentro de <dados_nao_confiaveis>.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus, urlsplit

import httpx

from services.controllers.charts import ChartError, validate_chart

logger = logging.getLogger("kira.app.info")

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_GAP_S = 6.0  # o GDELT recusa mais de um pedido a cada 5 s
GDELT_BACKOFF_S = 900.0  # recusou (429): usa só a reserva por 15 min
# Reserva do "mundo": a busca do Google Notícias em edições de vários países (o GDELT às vezes bloqueia o IP por horas)
WORLD_EDITIONS = (("en-US", "US", "US:en"), ("pt-BR", "BR", "BR:pt-419"), ("es-419", "MX", "MX:es-419"), ("en-GB", "GB", "GB:en"))
GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
CACHE_S = 600.0
MAX_ARTICLE_CHARS = 3500
ACTIONS = ("manchetes", "ler", "cotacao", "mundo")
OUTLETS = {
    "g1": ("g1", "https://g1.globo.com/rss/g1/"),
    "folha": ("Folha de S.Paulo", "https://feeds.folha.uol.com.br/emcimadahora/rss091.xml"),
    "bbc": ("BBC News Brasil", "https://feeds.bbci.co.uk/portuguese/rss.xml"),
    "cnn": ("CNN Brasil", "https://www.cnnbrasil.com.br/feed/"),
    "estadao": ("Estadão", "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/ultimas/"),
    "uol": ("UOL", "https://rss.uol.com.br/feed/noticias.xml"),
    "tecnoblog": ("Tecnoblog", "https://tecnoblog.net/feed/"),
    "canaltech": ("Canaltech", "https://canaltech.com.br/rss/"),
    "olhar digital": ("Olhar Digital", "https://olhardigital.com.br/feed/"),
    "tecmundo": ("Tecmundo", "https://rss.tecmundo.com.br/feed"),
    "the verge": ("The Verge", "https://www.theverge.com/rss/index.xml"),
    "techcrunch": ("TechCrunch", "https://techcrunch.com/feed/"),
    "ge": ("ge", "https://ge.globo.com/rss/ge/"),
    "diario do nordeste": ("Diário do Nordeste", "https://diariodonordeste.verdesmares.com.br/rss"),
    "o povo": ("O Povo", "https://www.opovo.com.br/rss.xml"),
    "nasa": ("NASA", "https://www.nasa.gov/feed/"),
}
# Nomes falados -> símbolo do Yahoo Finanças
SYMBOLS = {
    "dolar": "USDBRL=X", "dolar americano": "USDBRL=X", "euro": "EURBRL=X", "libra": "GBPBRL=X", "iene": "JPYBRL=X",
    "bitcoin": "BTC-USD", "btc": "BTC-USD", "ethereum": "ETH-USD", "ether": "ETH-USD", "solana": "SOL-USD",
    "ibovespa": "^BVSP", "bovespa": "^BVSP", "ibov": "^BVSP", "s&p": "^GSPC", "s&p 500": "^GSPC", "sp500": "^GSPC",
    "nasdaq": "^IXIC", "dow jones": "^DJI", "ouro": "GC=F", "petroleo": "BZ=F", "petroleo brent": "BZ=F",
    "petrobras": "PETR4.SA", "vale": "VALE3.SA", "itau": "ITUB4.SA", "bradesco": "BBDC4.SA", "banco do brasil": "BBAS3.SA",
    "magalu": "MGLU3.SA", "magazine luiza": "MGLU3.SA", "ambev": "ABEV3.SA", "weg": "WEGE3.SA", "nubank": "NU",
    "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "google": "GOOGL", "alphabet": "GOOGL", "amazon": "AMZN",
    "tesla": "TSLA", "meta": "META", "netflix": "NFLX",
}
B3_RE = re.compile(r"^[A-Za-z]{4}\d{1,2}$")
CONTROL_RE = re.compile(r"[\u0000-\u0008\u000b-\u001f\u007f​-‏‪-‮]")


def fold(text: object) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").lower())
    return " ".join("".join(ch for ch in raw if not unicodedata.combining(ch)).split())


def clean(text: object, limit: int = 300) -> str:
    return " ".join(CONTROL_RE.sub(" ", str(text or "")).split())[:limit]


def resolve_symbol(query: str) -> str | None:
    """'Petrobras', 'PETR4', 'dólar', 'bitcoin' -> símbolo; None = procurar no Yahoo."""
    text = fold(query).replace("acao da ", "").replace("acoes da ", "").replace("cotacao do ", "").replace("cotacao da ", "").strip()
    if text in SYMBOLS:
        return SYMBOLS[text]
    # "quanto tá o dólar hoje": o nome dentro da frase (o mais longo vence: "petroleo brent" antes de "petroleo")
    inside = [name for name in SYMBOLS if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", text)]
    if inside:
        return SYMBOLS[max(inside, key=len)]
    codes = [word for word in text.split() if B3_RE.fullmatch(word)]
    if codes:
        return codes[0].upper() + ".SA"
    compact = text.replace(" ", "")
    if B3_RE.fullmatch(compact):
        return compact.upper() + ".SA"
    if re.fullmatch(r"[a-z]{1,5}", compact) and compact.upper() == query.strip().upper():
        return compact.upper()
    return None


def pick_article(articles: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """'a segunda', '2', 'a do Galaxy': a matéria da última lista."""
    text = fold(ref)
    ordinals = {"primeira": 1, "segunda": 2, "terceira": 3, "quarta": 4, "quinta": 5, "sexta": 6, "setima": 7, "oitava": 8, "ultima": -1}
    number = next((value for word, value in ordinals.items() if re.search(rf"\b{word}\b", text)), None)
    digits = re.search(r"\b(\d{1,2})\b", text)
    if number is None and digits:
        number = int(digits.group(1))
    if number:
        index = number - 1 if number > 0 else len(articles) - 1
        return articles[index] if 0 <= index < len(articles) else None
    words = [w for w in re.findall(r"[a-z0-9]{4,}", text) if w not in {"essa", "esta", "aquela", "materia", "noticia", "sobre", "fala"}]
    scored = sorted(((sum(1 for w in words if w in fold(a.get("title"))), i) for i, a in enumerate(articles)), reverse=True)
    return articles[scored[0][1]] if scored and scored[0][0] > 0 else None


class InfoFeeds:
    def __init__(self, feeds_path: Path, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._feeds_path = Path(feeds_path)
        self._clock = clock
        self._cache: dict[str, tuple[float, Any]] = {}
        self._last_articles: list[dict[str, Any]] = []
        self._gdelt_at = 0.0
        self._gdelt_blocked_until = 0.0
        self._gdelt_lock = asyncio.Lock()

    def outlets(self) -> dict[str, tuple[str, str]]:
        """Veículos padrão + os do Chefe (configs/rss_feeds.json: {"nome": "url https do feed"})."""
        found = dict(OUTLETS)
        try:
            extra = json.loads(self._feeds_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            extra = {}
        if isinstance(extra, dict):
            for name, url in extra.items():
                if isinstance(name, str) and isinstance(url, str) and url.startswith("https://"):
                    found[fold(name)] = (clean(name, 40), url)
        return found

    def _cached(self, key: str) -> Any:
        hit = self._cache.get(key)
        return hit[1] if hit and self._clock() - hit[0] < CACHE_S else None

    # ---------- manchetes ----------
    async def headlines(self, source: str) -> dict[str, Any]:
        outlets = self.outlets()
        wanted = fold(source)
        key = wanted if wanted in outlets else next((k for k in outlets if k in wanted or wanted in k), None) if wanted else None
        if key is None:
            return {"ok": False, "erro": f"não conheço o veículo '{clean(source, 40)}'", "veiculos": sorted(v[0] for v in outlets.values())}
        name, url = outlets[key]
        cached = self._cached(f"rss:{url}")
        if cached is None:
            import feedparser

            parsed = await asyncio.to_thread(feedparser.parse, url, agent="KiraAssistente/1.0 (uso pessoal)")
            cached = []
            for entry in parsed.entries[:12]:
                link = str(entry.get("link") or "")
                if not link.startswith("https://"):
                    continue
                stamp = entry.get("published_parsed") or entry.get("updated_parsed")
                cached.append({"title": clean(entry.get("title"), 200), "canonicalUrl": link, "publisher": name,
                               "publishedAt": datetime(*stamp[:6], tzinfo=timezone.utc).isoformat() if stamp else None,
                               "summary": clean(re.sub(r"<[^>]+>", " ", str(entry.get("summary") or "")), 220)})
            self._cache[f"rss:{url}"] = (self._clock(), cached)
        if not cached:
            return {"ok": False, "erro": f"o feed do {name} não respondeu agora"}
        self._last_articles = cached
        return {"ok": True, "veiculo": name, "manchetes": [{"n": i + 1, "titulo": a["title"]} for i, a in enumerate(cached[:8])], "artigos": cached[:8]}

    # ---------- ler matéria ----------
    async def read(self, ref: str) -> dict[str, Any]:
        text = str(ref or "").strip()
        article = {"canonicalUrl": text, "title": "", "publisher": urlsplit(text).hostname or ""} if text.startswith("https://") else pick_article(self._last_articles, text)
        if not article:
            return {"ok": False, "erro": "não sei qual matéria: peça as manchetes primeiro ou diga o número ou um assunto do título"}
        import trafilatura

        def fetch_and_extract() -> tuple[str, str]:
            html = trafilatura.fetch_url(article["canonicalUrl"])
            if not html:
                return "", ""
            body = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
            meta = trafilatura.extract_metadata(html)
            return body, (getattr(meta, "title", "") or "") if meta else ""

        body, title = await asyncio.to_thread(fetch_and_extract)
        if not body:
            return {"ok": False, "erro": "a página da matéria não abriu ou não tem texto"}
        return {"ok": True, "titulo": clean(title or article.get("title"), 200), "veiculo": clean(article.get("publisher"), 60),
                "texto": clean(body, MAX_ARTICLE_CHARS), "link": article["canonicalUrl"], "artigos": [article]}

    # ---------- cotação ----------
    async def quote(self, query: str) -> dict[str, Any]:
        symbol = resolve_symbol(query)
        import yfinance as yf

        if symbol is None:
            def search() -> str | None:
                quotes = yf.Search(query, max_results=5).quotes
                return next((q.get("symbol") for q in quotes if q.get("symbol") and q.get("quoteType") in {"EQUITY", "ETF", "INDEX", "CRYPTOCURRENCY", "CURRENCY", "FUTURE"}), None)

            symbol = await asyncio.to_thread(search)
        if not symbol:
            return {"ok": False, "erro": f"não achei cotação para '{clean(query, 40)}'"}
        cached = self._cached(f"quote:{symbol}")
        if cached is not None:
            return cached

        def load() -> dict[str, Any]:
            ticker = yf.Ticker(symbol)
            history = ticker.history(period="1mo", interval="1d")
            if history is None or history.empty:
                return {"ok": False, "erro": f"sem preço para {symbol} agora"}
            closes = [float(v) for v in history["Close"].tolist()]
            dates = [d.strftime("%d/%m") for d in history.index]
            meta = getattr(ticker, "history_metadata", None) or {}
            last, previous = closes[-1], closes[-2] if len(closes) > 1 else closes[-1]
            return {"ok": True, "simbolo": symbol, "nome": clean(meta.get("longName") or meta.get("shortName") or symbol, 60),
                    "moeda": meta.get("currency") or "", "preco": round(last, 4 if last < 10 else 2),
                    "variacao_dia_pct": round((last - previous) / previous * 100, 2) if previous else None,
                    "variacao_mes_pct": round((last - closes[0]) / closes[0] * 100, 2) if closes[0] else None,
                    "minimo_mes": round(min(closes), 2), "maximo_mes": round(max(closes), 2), "data": dates[-1],
                    "serie": {"datas": dates, "fechamentos": [round(c, 4) for c in closes]}}

        facts = await asyncio.to_thread(load)
        if facts.get("ok"):
            self._cache[f"quote:{symbol}"] = (self._clock(), facts)
        return facts

    # ---------- o mundo ----------
    async def world(self, query: str) -> dict[str, Any]:
        topic = clean(query, 80)
        if len(topic) < 3:
            return {"ok": False, "erro": "diga o assunto"}
        cached = self._cached(f"gdelt:{fold(topic)}")
        if cached is not None:
            self._last_articles = cached.get("artigos") or []
            return cached
        articles: list[dict[str, Any]] = []
        source = "GDELT"
        for language_filter in (" sourcelang:por", "") if self._clock() >= self._gdelt_blocked_until else ():
            data = await self._gdelt(f"{topic}{language_filter}")
            if self._clock() < self._gdelt_blocked_until:
                break  # recusou: vai direto para a reserva
            for item in (data.get("articles") or [])[:10]:
                url = str(item.get("url") or "")
                if not url.startswith("https://"):
                    continue
                seen = str(item.get("seendate") or "")
                when = f"{seen[0:4]}-{seen[4:6]}-{seen[6:8]}T{seen[9:11]}:{seen[11:13]}:00+00:00" if len(seen) >= 15 else None
                articles.append({"title": clean(item.get("title"), 200), "canonicalUrl": url, "publisher": clean(item.get("domain"), 60),
                                 "publishedAt": when, "summary": clean(f"{item.get('sourcecountry') or ''} · {item.get('language') or ''}", 80)})
            if articles:
                break
        if not articles:
            source = "Google Notícias (EUA, Brasil, México, Reino Unido)"
            articles = await self._world_by_editions(topic)
        if not articles:
            return {"ok": False, "erro": "não achei notícias sobre isso nas últimas 48 horas"}
        countries = sorted({a["summary"].split(" · ")[0] for a in articles if a["summary"].split(" · ")[0]})
        facts = {"ok": True, "assunto": topic, "fonte": source, "quantas": len(articles), "paises": countries[:8],
                 "manchetes": [{"n": i + 1, "titulo": a["title"], "fonte": a["publisher"]} for i, a in enumerate(articles)], "artigos": articles}
        self._cache[f"gdelt:{fold(topic)}"] = (self._clock(), facts)
        self._last_articles = articles
        return facts

    async def _gdelt(self, query: str) -> dict[str, Any]:
        async with self._gdelt_lock:
            wait = GDELT_GAP_S - (self._clock() - self._gdelt_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._gdelt_at = self._clock()
            async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "KiraAssistente/1.0"}) as client:
                response = await client.get(GDELT_URL, params={"query": query, "mode": "artlist", "maxrecords": 10, "format": "json",
                                                               "timespan": "48h", "sort": "datedesc"})
            if response.status_code != 200:
                logger.info("INFO_GDELT_STATUS status=%s", response.status_code)
                if response.status_code == 429:
                    self._gdelt_blocked_until = self._clock() + GDELT_BACKOFF_S
                return {}
            try:
                return response.json()
            except ValueError:  # sem resultado o GDELT devolve texto vazio
                return {}

    async def _world_by_editions(self, topic: str) -> list[dict[str, Any]]:
        import feedparser

        async def edition(hl: str, gl: str, ceid: str) -> list[dict[str, Any]]:
            url = f"{GOOGLE_NEWS_URL}?q={quote_plus(topic + ' when:2d')}&hl={hl}&gl={gl}&ceid={ceid}"
            parsed = await asyncio.to_thread(feedparser.parse, url, agent="KiraAssistente/1.0 (uso pessoal)")
            found = []
            for entry in parsed.entries[:3]:
                link = str(entry.get("link") or "")
                if not link.startswith("https://"):
                    continue
                stamp = entry.get("published_parsed")
                outlet = clean((entry.get("source") or {}).get("title") if isinstance(entry.get("source"), dict) else "", 60)
                found.append({"title": clean(re.sub(r"\s+-\s+[^-]+$", "", str(entry.get("title") or "")), 200), "canonicalUrl": link,
                              "publisher": outlet or gl, "publishedAt": datetime(*stamp[:6], tzinfo=timezone.utc).isoformat() if stamp else None,
                              "summary": clean(f"{gl} · {hl}", 40)})
            return found

        groups = await asyncio.gather(*(edition(*spec) for spec in WORLD_EDITIONS), return_exceptions=True)
        return [item for group in groups if isinstance(group, list) for item in group][:12]

    async def ask(self, action: str, query: str = "", source: str = "") -> dict[str, Any]:
        try:
            if action == "manchetes":
                facts = await self.headlines(source or query)
            elif action == "ler":
                facts = await self.read(query)
            elif action == "cotacao":
                facts = await self.quote(query)
            else:
                facts = await self.world(query)
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            logger.info("INFO_SOURCE_FAILED action=%s reason=%s", action, type(exc).__name__)
            facts = {"ok": False, "erro": "a fonte não respondeu agora"}
        facts["acao"] = action
        return facts


def build_presentation(facts: dict[str, Any]) -> dict[str, Any] | None:
    action = facts.get("acao")
    if not facts.get("ok"):
        return None
    request_id = f"info-{uuid.uuid4().hex[:10]}"
    sections: list[dict[str, Any]] = []
    if action in {"manchetes", "mundo"}:
        title = f"Manchetes: {facts['veiculo']}" if action == "manchetes" else f"No mundo: {facts['assunto']}"
        articles = [{k: v for k, v in a.items() if v} for a in facts.get("artigos") or []]
        sections.append({"id": "manchetes", "type": "news", "heading": title, "status": "ready", "data": {"articles": articles}})
    elif action == "ler":
        title = facts.get("titulo") or "Matéria"
        paragraphs = [p for p in re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ])", facts["texto"])]
        sections.append({"id": "texto", "type": "text", "heading": facts.get("veiculo") or "Matéria", "status": "ready",
                         "data": {"paragraphs": [" ".join(paragraphs[i:i + 3]) for i in range(0, min(len(paragraphs), 18), 3)]}})
    elif action == "cotacao":
        title = f"{facts['nome']} ({facts['simbolo']})"
        day = facts.get("variacao_dia_pct")
        body = f"{facts['preco']} {facts['moeda']} em {facts['data']}" + (f", {'+' if day >= 0 else ''}{day}% no dia" if day is not None else "")
        sections.append({"id": "preco", "type": "text", "heading": "Agora", "status": "ready",
                         "data": {"body": body, "points": [f"No mês: {facts['variacao_mes_pct']}%", f"Mínimo e máximo do mês: {facts['minimo_mes']} a {facts['maximo_mes']}"]}})
        try:
            chart = validate_chart({"chartId": "cotacao", "chartType": "line", "title": f"Fechamento diário: {facts['simbolo']}",
                                    "xAxis": {"label": "dia", "labels": facts["serie"]["datas"]}, "yAxis": {"label": "preço"},
                                    "series": [{"name": facts["simbolo"], "unit": facts["moeda"] or "", "data": facts["serie"]["fechamentos"][-40:]}],
                                    "sourceIds": ["yahoo"]})
            if len(chart["xAxis"]["labels"]) == len(chart["series"][0]["data"]):
                sections.append({"id": "grafico", "type": "chart", "heading": "Último mês", "status": "ready", "data": chart})
        except ChartError:
            pass
    else:
        return None
    sources = []
    if action == "cotacao":
        sources.append({"id": "yahoo", "name": "Yahoo Finanças", "url": f"https://finance.yahoo.com/quote/{facts['simbolo']}"})
    elif action == "mundo":
        sources.append({"id": "gdelt", "name": "GDELT Project", "url": "https://www.gdeltproject.org"})
    elif action == "ler" and str(facts.get("link") or "").startswith("https://"):
        sources.append({"id": "materia", "name": facts.get("veiculo") or "Matéria", "url": facts["link"]})
    return {"type": "present", "presentationId": "info-kira", "requestId": request_id, "title": clean(title, 120), "status": "ready",
            "sections": sections, "sources": sources}


def narration(facts: dict[str, Any]) -> str:
    if not facts.get("ok"):
        extra = f" Veículos que conheço: {', '.join(facts['veiculos'])}." if facts.get("veiculos") else ""
        return f"Não deu: {facts.get('erro') or 'a fonte não respondeu agora'}.{extra} Diga isso com naturalidade."
    action = facts.get("acao")
    if action == "ler":
        return ("Resuma esta matéria em 4 a 6 frases, com suas palavras, citando o veículo; o texto completo está na tela. "
                f"<dados_nao_confiaveis>{json.dumps({k: facts[k] for k in ('titulo', 'veiculo', 'texto')}, ensure_ascii=False)}</dados_nao_confiaveis>")
    keep = {k: v for k, v in facts.items() if k not in {"ok", "artigos", "serie", "acao"}}
    lead = {"manchetes": "Conte 3 ou 4 manchetes, curtas, e ofereça ler alguma (pelo número).",
            "mundo": "Conte o que o mundo está noticiando (3 ou 4 manchetes, de onde vêm) e ofereça ler alguma.",
            "cotacao": "Diga o preço, a variação do dia e do mês, curto."}.get(action, "")
    return f"{lead} <dados_nao_confiaveis>{json.dumps(keep, ensure_ascii=False)[:2500]}</dados_nao_confiaveis>"


__all__ = ["ACTIONS", "InfoFeeds", "build_presentation", "narration", "pick_article", "resolve_symbol"]
