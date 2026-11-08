"""Function tools de mídia que conversam somente com o backend local da Kira."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import time
from urllib.parse import urlsplit

import httpx
from livekit.agents import function_tool
from livekit.agents.llm import StopResponse

from services.media_topics import topic_is_searchable


_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$", re.IGNORECASE)
_KINDS = frozenset({"images", "videos", "articles"})
_SHOW_MEDIA_COOLDOWN_SECONDS = 20.0
_last_show_media_at = 0.0
_SCORE_TIMEOUT_SECONDS = 6.5
_SCORE_UNAVAILABLE = "Não tenho placar ao vivo confiável agora."


UI_TOOLS_ANSWERED_EARLY = os.getenv("KIRA_GEMINI_UI_TOOLS_NON_BLOCKING", "1").strip().lower() not in {"0", "false", "no", "off"}
# Estas duas tools precisam devolver o resultado real para o modelo: a aparência
# só é sucesso após ACK da interface e painel precisa dizer se a rota aplicou.
UI_TOOLS_REQUIRE_RESULT = frozenset({"aparencia", "painel"})


def ui_tool(fn):
    """Tool visual silenciosa ou confirmável, conforme o nome da função."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        result = await fn(*args, **kwargs)
        if UI_TOOLS_ANSWERED_EARLY and fn.__name__ not in UI_TOOLS_REQUIRE_RESULT:
            raise StopResponse()
        return result

    return wrapper


def _local_backend_url(path: str) -> str:
    configured = str(os.getenv("KIRA_UI_BASE_URL") or "http://127.0.0.1:3000").strip().rstrip("/")
    try:
        parsed = urlsplit(configured)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            configured = "http://127.0.0.1:3000"
    except Exception:
        configured = "http://127.0.0.1:3000"
    return f"{configured}{path}"


async def _get_local(path: str, *, read_timeout: float = 20.0) -> dict[str, object]:
    token = str(os.getenv("KIRA_INTERNAL_TOKEN") or "").strip()
    timeout = httpx.Timeout(connect=0.35, read=read_timeout, write=0.35, pool=0.35)
    async with httpx.AsyncClient(timeout=timeout, headers={"X-Kira-Internal": token}, verify=False) as client:
        response = await asyncio.wait_for(client.get(_local_backend_url(path)), timeout=read_timeout + 0.6)
    try:
        body = response.json()
    except (TypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


async def _post_local(path: str, payload: dict[str, object], *, read_timeout: float = 1.2) -> dict[str, object]:
    started = time.monotonic()
    token = str(os.getenv("KIRA_INTERNAL_TOKEN") or "").strip()
    headers = {"X-Kira-Internal": token, "Content-Type": "application/json"}
    timeout = httpx.Timeout(connect=0.35, read=read_timeout, write=0.35, pool=0.35)
    async with httpx.AsyncClient(timeout=timeout, headers=headers, verify=False) as client:
        response = await asyncio.wait_for(client.post(_local_backend_url(path), json=payload), timeout=read_timeout + 0.6)
    if response.status_code >= 400:
        try:
            error = response.json().get("error")
        except (TypeError, ValueError, AttributeError):
            error = None
        body = {"ok": False, "status": response.status_code, "error": error}
        _record_backend_outcome(path, payload, body, started)
        return body
    try:
        body = response.json()
    except (TypeError, ValueError):
        body = {"ok": False, "status": response.status_code}
        _record_backend_outcome(path, payload, body, started)
        return body
    normalized = body if isinstance(body, dict) else {"ok": False, "status": response.status_code}
    _record_backend_outcome(path, payload, normalized, started)
    return normalized


def _record_backend_outcome(path: str, payload: dict[str, object], result: dict[str, object], started: float) -> None:
    """Aprende com endpoints que confirmam sucesso/falha, sem atrasar o retorno."""
    try:
        from services.experience_memory import record_tool_backend_attempt

        record_tool_backend_attempt(
            path,
            payload,
            result,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            source="tool_backend",
        )
    except Exception:
        # Aprendizado é observacional: nunca pode quebrar a ferramenta chamada.
        return None


def _normalized_kinds(value: object) -> list[str]:
    if value is None:
        return ["images"]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        kind = str(item or "").strip().lower()
        if kind in _KINDS and kind not in result:
            result.append(kind)
    return result


async def _media_summary(path: str, payload: dict[str, object]) -> str:
    try:
        result = await _post_local(path, payload, read_timeout=3.0)
    except Exception:
        return "Não consegui falar com o visualizador agora."
    return " ".join(str(result.get("summary") or "").split())[:400] or "Não consegui falar com o visualizador agora."


async def request_media(topic: str, kinds: object = None, image_query: str | None = None) -> str:
    """Agenda a pesquisa de midia no backend; usado pela tool e pelo gatilho automatico."""
    global _last_show_media_at
    normalized_topic = " ".join(str(topic or "").split())[:180].strip()
    requested_kinds = _normalized_kinds(kinds)
    if not topic_is_searchable(normalized_topic) or not requested_kinds:
        return "Não identifiquei um assunto para pesquisar; nada foi aberto."
    now = time.monotonic()
    if now - _last_show_media_at < _SHOW_MEDIA_COOLDOWN_SECONDS:
        return "Já estou pesquisando mídia sobre um tema recente; aguarde um instante."
    _last_show_media_at = now
    try:
        result = await _post_local(
            "/api/ui/media/show",
            {
                "topic": normalized_topic,
                "kinds": requested_kinds,
                "image_query": str(image_query or "").strip()[:180] or None,
                "language": "pt",
            },
        )
    except Exception:
        return "Não consegui iniciar a pesquisa de mídia agora."
    if result.get("disabled"):
        return "A mídia automática está desativada pelo usuário."
    if result.get("ok") and result.get("accepted"):
        return "ok"
    return "Não consegui iniciar a pesquisa de mídia agora."


async def _score_request(path: str, payload: dict[str, object]) -> str:
    """Sempre devolve ao Gemini um resumo curto: sucesso, sem jogo ou indisponivel."""
    try:
        result = await _post_local(path, payload, read_timeout=_SCORE_TIMEOUT_SECONDS)
    except Exception:
        return _SCORE_UNAVAILABLE
    summary = " ".join(str(result.get("summary") or "").split())[:400]
    return summary or _SCORE_UNAVAILABLE


class MediaToolsMixin:
    @function_tool(
        name="show_media",
        description=(
            "Inicia uma explicação visual sobre um assunto: abre UM visualizador com a primeira imagem "
            "(artigo só se o usuário pedir). topic: o assunto da conversa (nunca negações ou palavras soltas). Para narrar com imagens "
            "por momento, use plan_visuals. Vídeos só se o usuário pedir (kinds=['videos']). Chame no mesmo turno, antes de falar."
        ),
    )
    @ui_tool
    async def show_media(
        self,
        topic: str,
        kinds: list[str] | None = None,
        image_query: str | None = None,
    ) -> str:
        return await request_media(topic, kinds, image_query)

    @function_tool(
        name="show_live_score",
        description=(
            "Mostra o placar de um time de futebol (ao vivo, ou último e próximo jogo se não houver jogo hoje) e "
            "devolve um resumo curto para você narrar. Use quando pedirem placar, resultado ou como está o jogo. "
            "Passe o nome do time exatamente como o usuário disse. Nunca invente placar."
        ),
    )
    async def show_live_score(self, team: str) -> str:
        normalized_team = " ".join(str(team or "").split())[:80].strip()
        if not normalized_team:
            return "Não consegui identificar o time."
        return await _score_request("/api/ui/score/show", {"team": normalized_team})

    @function_tool(
        name="show_scores_today",
        description="Mostra os jogos de futebol de hoje e devolve um resumo curto. Use para 'quais jogos têm hoje' sem time específico.",
    )
    async def show_scores_today(self) -> str:
        return await _score_request("/api/ui/score/today", {})

    @function_tool(
        name="plan_visuals",
        description=(
            "Antes da primeira frase de uma história ou explicação visual, declare UMA vez de 3 a 6 momentos, na ordem em "
            "que vai falar. topic: o assunto. beats: um texto por momento que sirva sozinho como busca de imagem e use as "
            "palavras que você vai dizer (ex.: ['Desembarque de Cabral em Porto Seguro', 'Povos indígenas tupiniquins', "
            "'Primeira missa no Brasil', 'Carta de Pero Vaz de Caminha']). Fale de cada momento nessa ordem: a imagem dele "
            "aparece quando você disser essas palavras. Não fale sobre esta ferramenta."
        ),
    )
    @ui_tool
    async def plan_visuals(self, topic: str, beats: list[str]) -> str:
        cleaned = " ".join(str(topic or "").split())[:160]
        names = [" ".join(str(b or "").split())[:90] for b in (beats or [])]
        moments = [{"beat": name, "query": name, "caption": name} for name in names if name][:6]
        if not topic_is_searchable(cleaned) or not moments:
            return "Não identifiquei o que mostrar; nada foi aberto."
        return await _media_summary("/api/ui/media/plan", {"topic": cleaned, "beats": moments})

    @function_tool(
        name="recall_image",
        description="Reabre no visualizador uma imagem que já foi mostrada nesta sessão (por descrição), sem nova busca.",
    )
    @ui_tool
    async def recall_image(self, description: str) -> str:
        return await _media_summary("/api/ui/media/recall", {"description": str(description or "")[:160]})

    @function_tool(
        name="list_active_panels",
        description=(
            "O que está na tela agora, separado por janela: painéis da Kira (assunto, legendas das imagens, Sistema Vital) "
            "e as janelas extras (à mostra, minimizada ou coberta, com os widgets). Em cada janela o mais recente fica em "
            "destaque e os anteriores em miniatura ao lado. Use antes de responder sobre 'essa imagem', 'esse painel', "
            "'o que tem na tela' ou 'na outra janela'."
        ),
    )
    async def list_active_panels(self) -> str:
        return await _media_summary("/api/ui/media/panels", {})

    @function_tool(
        name="close_panels",
        description=(
            "Fecha painéis da Kira. target: 'tudo' (padrão), um tipo ('imagens', 'artigo', 'placar', 'eleição', 'download', "
            "'sistema vital') ou parte do assunto. Para APIs ativas, transcrição, relógio e botões use a tool 'painel'. Use quando o usuário disser 'pode fechar', 'já pode ocultar', 'não precisa mais', 'tá bom, pode tirar'."
        ),
    )
    @ui_tool
    async def close_panels(self, target: str = "tudo") -> str:
        # Painéis fixos da tela principal não são mídia: "fecha/oculta as APIs" caía aqui e voltava "ok" sem esconder
        # nada (2026-10-31). Esses vão para a rota de visibilidade, e o resultado real volta para a Kira.
        fixed = _fixed_panel_for(target)
        if fixed and fixed != "all":  # "fecha todos os painéis" fecha mídia/painéis, nunca esconde a transcrição
            try:
                result = await _post_local("/api/ui/panel-visibility", {"panel": fixed, "visible": False, "source": "close_panels"})
            except Exception:
                return "Não consegui esconder esse painel agora."
            return "ok" if result.get("ok") else "Não consegui esconder esse painel agora."
        try:
            result = await _post_local("/api/ui/media/close", {"target": str(target or "tudo")[:60]})
        except Exception:
            return "Não consegui fechar as mídias agora."
        if not result.get("ok"):
            return "Não consegui fechar as mídias agora."
        return "ok"

    @function_tool(
        name="painel",
        description=(
            "Painéis fixos da tela principal da Kira: transcrição, APIs ativas, relógio e os botões da lateral. "
            "acao: 'mostrar', 'ocultar', 'mover' ou 'tamanho'. painel: 'transcricao', 'apis', 'relogio', 'botoes', 'sistema vital' "
            "ou 'tudo'. onde (para mover): 'esquerda', 'direita', 'centro', 'topo', 'baixo', 'canto superior esquerdo', "
            "'canto superior direito', 'canto inferior esquerdo', 'canto inferior direito' ou 'padrao'. tamanho: 'maior', 'menor', "
            "'grande', 'medio', 'pequeno' ou 'padrao'. Use sempre que o usuário pedir para esconder, mostrar, mover, aumentar ou "
            "diminuir esses painéis, mesmo se a frase vier truncada (ex.: 'oculta as APIs', 'tira o painel de ativos', 'joga a "
            "transcrição pra direita'). Só diga que fez se a resposta for 'ok'."
        ),
    )
    @ui_tool
    async def painel(self, acao: str = "ocultar", painel: str = "apis", onde: str = "", tamanho: str = "") -> str:
        action = str(acao or "").strip().lower()
        name = str(painel or "").strip().lower()
        if action in {"mostrar", "ocultar", "esconder", "exibir"}:
            fixed = "all" if name in {"tudo", "todos", "all"} else _fixed_panel_for(name)
            if not fixed:
                return "Não sei qual painel é esse. Posso mexer em transcrição, APIs, relógio e Sistema Vital."
            try:
                result = await _post_local("/api/ui/panel-visibility", {"panel": fixed, "visible": action in {"mostrar", "exibir"}, "source": "tool_painel"})
            except Exception:
                return "Não consegui mexer nesse painel agora."
            return "ok" if result.get("ok") else "Não consegui mexer nesse painel agora."
        key = _layout_key_for(name)
        if not key:
            return "Esse painel não dá para mover. Dá para mover transcrição, APIs, relógio e os botões da lateral."
        try:
            result = await _post_local("/api/ui/panel-layout", {"panel": key, "where": str(onde or "").strip().lower(), "size": str(tamanho or "").strip().lower()})
        except Exception:
            return "Não consegui mover esse painel agora."
        return "ok" if result.get("ok") else "Não entendi para onde ou de que tamanho. " + json.dumps({k: result.get(k) for k in ("where", "size")}, ensure_ascii=False)

    @function_tool(
        name="aparencia",
        description=(
            "Muda a aparência da Kira na hora, sem reiniciar. Preencha só o que o usuário pediu. "
            "orbe: nuvem (de partículas), colmeia (geodésica), voxels, globo, galaxia, circulo, particulas, constelacao, colmeia, buraco negro, aneis de dados, plasma, toro, fitas, "
            "quatro juntos (ou 'proximo' para trocar para outro). cor: roxo, ciano, rosa, ambar. fundo: galaxia, preto, grade. "
            "relogio: padrao, minimo, neon, vidro, terminal, analogico, oculto. menu: padrao, pilula, trilho, apps. "
            "chat: padrao, orbe. Use para 'troca o orbe', 'põe o buraco negro', 'muda a cor pra ciano', 'coloca o relógio neon'. "
            "Só diga que trocou se a resposta tiver applied=true (a tela precisa confirmar; broadcast sem tela não conta)."
        ),
    )
    @ui_tool
    async def aparencia(self, orbe: str = "", cor: str = "", fundo: str = "", relogio: str = "", menu: str = "", chat: str = "") -> str:
        patch = {key: _APPEARANCE_ALIASES.get(_plain_word(value), _plain_word(value))
                 for key, value in (("orb", orbe), ("accent", cor), ("bg", fundo), ("clock", relogio), ("menu", menu), ("chat", chat)) if str(value or "").strip()}
        if patch.get("orb") == "proximo":
            patch["orb"] = _ORB_CYCLE[(_ORB_CYCLE.index(_last_orb[0]) + 1) % len(_ORB_CYCLE)] if _last_orb[0] in _ORB_CYCLE else "constellation"
        if "orb" in patch and patch["orb"] in _ORB_CYCLE:
            _last_orb[0] = patch["orb"]
        try:
            result = await _post_local("/api/ui/appearance", patch)
        except Exception:
            return "Não consegui mudar a aparência agora."
        if result.get("ok") and result.get("applied") is True:
            # "ok" seco deixava o Gemini calado: pede uma confirmação curta em voz.
            return "Trocado na tela (" + ", ".join(patch.values()) + "). Confirme em voz numa frase bem curta, tipo 'Pronto, coloquei o buraco negro'."
        if result.get("reason") == "ui_not_connected":
            return "Não consegui confirmar a mudança: a tela da Kira está desconectada. Não diga que trocou."
        if result.get("reason") == "ui_ack_timeout":
            return "A tela não confirmou a mudança a tempo. Não diga que trocou; peça para conferir a interface."
        if result.get("applied") is not True:
            return "Não consegui confirmar a mudança na tela. Não diga que trocou."
        return "Opção desconhecida. " + json.dumps({k: result.get(k) for k in ("field", "allowed")}, ensure_ascii=False)

    @function_tool(
        name="celular",
        description=(
            "Acesso à Kira pelo celular. acao: 'ligar' (liga o acesso e o link fixo), 'desligar', 'link publico' (liga o link "
            "que funciona pelos dados móveis), 'sem link publico', 'rede de casa' (liga entrar pelo Wi-Fi de casa), "
            "'sem rede de casa', 'status' (diz se está ligado e qual é o link). Responda com o link quando houver."
        ),
    )
    async def celular(self, acao: str = "status") -> str:
        action = _plain_word(acao)
        change = {
            "ligar": {"enabled": True}, "desligar": {"enabled": False},
            "link publico": {"public_link": True}, "sem link publico": {"public_link": False},
            "rede de casa": {"lan": True}, "sem rede de casa": {"lan": False},
        }.get(action)
        try:
            if change is None:
                result = await _get_local("/api/remote/settings")
                data = result.get("data") or result
            else:
                result = await _post_local("/api/remote/settings", change, read_timeout=40.0)
                if not result.get("success"):
                    return str(result.get("error") or "Não consegui mudar o acesso pelo celular.")
                data = result.get("data") or {}
        except Exception:
            return "Não consegui falar com o acesso pelo celular agora."
        links = data.get("links") or {}
        tail = data.get("tailscale") or {}
        parts = [f"acesso {'ligado' if data.get('running') else 'desligado'}"]
        if links.get("fixo"):
            parts.append(f"link: {links['fixo']}")
        if links.get("casa"):
            parts.append(f"rede de casa: {links['casa']}")
        if data.get("public_link"):
            parts.append("link público ligado")
        if not tail.get("running"):
            parts.append("o Tailscale está desligado no PC (o link fixo volta sozinho quando ele ligar)")
        if tail.get("error"):
            parts.append(f"Tailscale: {tail['error']}")
        return "; ".join(parts)

    @function_tool(
        name="focus_media",
        description="Foca uma imagem específica no painel de mídia da Kira.",
    )
    @ui_tool
    async def focus_media(self, panel_id: str, index: int = 0) -> str:
        normalized_id = str(panel_id or "").strip()
        if not _ID_RE.fullmatch(normalized_id) or not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 10000:
            return "Não consegui identificar esse item de mídia."
        try:
            result = await _post_local(
                "/api/ui/media/focus",
                {"panel_id": normalized_id, "index": index},
            )
        except Exception:
            return "Não consegui focar esse item de mídia agora."
        return "ok" if result.get("ok") else "Não consegui focar esse item de mídia agora."


_FIXED_PANELS = (
    (("api", "apis", "ativos", "ativas", "livekit", "gemini"), "api_identity"),
    (("transcri", "historico", "histórico"), "transcription"),
    (("vital", "cpu", "ram"), "system_vitals"),
)


def _fixed_panel_for(target: object) -> str | None:
    """'apis ativas', 'painel de ativos', 'transcrição', 'sistema vital' -> nome do painel fixo; senão None."""
    text = str(target or "").lower()
    for words, panel in _FIXED_PANELS:
        if any(word in text for word in words):
            return panel
    return None


_ORB_CYCLE = ["nuvem", "colmeia", "voxels", "galaxy", "particles", "constellation", "hive", "ring", "datarings", "fusion", "plasma", "torus", "ribbons", "blackhole"]
_last_orb = ["galaxy"]
# palavra falada (sem acento) -> valor de appearance.js
_APPEARANCE_ALIASES = {
    "galaxia": "galaxy", "circulo": "ring", "anel": "ring", "particulas": "particles", "constelacao": "constellation",
    "colmeia": "colmeia", "colmeia geodesica": "colmeia", "colmeia antiga": "hive", "nuvem": "nuvem", "nuvem de particulas": "nuvem",
    "voxels": "voxels", "voxel": "voxels", "cubinhos": "voxels", "globo": "globo", "globo 3d": "globo", "buraco negro": "blackhole", "aneis de dados": "datarings", "aneis": "datarings", "plasma": "plasma",
    "nucleo de plasma": "plasma", "toro": "torus", "toro de luz": "torus", "fitas": "ribbons", "fitas quanticas": "ribbons",
    "quatro juntos": "fusion", "os quatro juntos": "fusion", "fusao": "fusion", "proximo": "proximo", "outro": "proximo",
    "preto": "black", "grade": "grid", "ambar": "ambar", "laranja": "ambar", "azul": "ciano", "roxo": "roxo", "rosa": "rosa",
    "analogico": "analogico", "nenhum": "oculto", "sem relogio": "oculto", "esconder": "oculto", "pilula": "pilula",
    "trilho": "trilho", "icones": "apps", "icones de app": "apps", "sobre o orbe": "orbe", "normal": "padrao", "original": "padrao",
}


def _plain_word(value: object) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    return " ".join("".join(ch for ch in text if not unicodedata.combining(ch)).split())


def _layout_key_for(name: str) -> str | None:
    """Nome falado -> chave do panelDrag (shell/panelDrag.js)."""
    text = str(name or "").lower()
    if "transcri" in text or "histor" in text:
        return "transcript"
    if "api" in text or "ativ" in text:
        return "apis"
    if "relog" in text or "relóg" in text or "hora" in text:
        return "clock"
    if "bot" in text or "lateral" in text:
        return "side"
    return None


__all__ = ["MediaToolsMixin"]
