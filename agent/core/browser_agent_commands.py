"""Browser Intent Gate (Fase 2.5).

Detecta quando o usuario quer uma tarefa de NAVEGADOR (abrir site, pesquisar,
multi-step, login, formulario) e tambem frases de CONTINUACAO de um job ativo
(input/secret/user-action/confirm/cancel).

NAO transforma todo comando em browser. NAO casa documentos/pasta/artifact/
PDF/WhatsApp (esses tem gates proprios antes/depois).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata

logger = logging.getLogger("agent.core.browser_agent_commands")


@dataclass
class BrowserAgentCommand:
    matched: bool
    action: str = ""              # navigate | search | multistep
    browser_goal: str = ""
    url: str = ""
    query: str = ""
    requires_multistep: bool = False
    target_service: str = ""
    confidence: float = 0.0
    text_only_ack: str = ""
    goal_intent: str = "unknown"  # observe_page | login_assisted | fill_form | search | navigate | unknown
    auth_allowed: bool = False    # so True quando o usuario pede login/entrar na conta explicitamente


@dataclass
class BrowserFollowup:
    matched: bool
    kind: str = ""                # input | input_with_secret | secret | save | no_save | show | user_action_done | confirm | decline | cancel | youtube_choice | youtube_scroll | youtube_control
    value: str = ""
    save: bool = False
    secret: str = ""              # FASE 3.2D-HOTFIX: senha extraida junto com o e-mail (combo)
    command: str = ""             # FASE 1 (agente YouTube): comando de controle do video (kind=youtube_control)


_KNOWN_SITES = {
    "google": "https://www.google.com",
    "facebook": "https://www.facebook.com",
    "gmail": "https://mail.google.com",
    "youtube": "https://www.youtube.com",
    "instagram": "https://www.instagram.com",
    "twitter": "https://twitter.com",
    "x": "https://twitter.com",
    "linkedin": "https://www.linkedin.com",
    "wikipedia": "https://pt.wikipedia.org",
    "github": "https://github.com",
}

_URL_RE = re.compile(r"(https?://[^\s]+|www\.[^\s]+|[a-z0-9-]+\.(?:com|com\.br|org|net|gov|io|dev|ai)(?:/[^\s]*)?)", re.IGNORECASE)
_NEGATIVE = ("meus documentos", "minhas imagens", "downloads", "area de trabalho", "explorador",
             "gerenciador de arquivos", "pasta do", "pasta de", "ultimo arquivo", "ultimo pdf",
             "ultimo texto", "cade ", "que voce criou", "whatsapp", "zap ", " no zap")
_MULTISTEP_HINTS = ("login", "logar", "entrar na conta", "faz login", "fazer login", "preench", "formulario",
                    "formulário", "me diz", "do que se trata", "do que ele trata", "tenta entrar", "area de contato",
                    "área de contato", "botao de inscricao", "botão de inscrição", "ve se tem", "vê se tem",
                    "ve do que", "vê do que", "entra nesse site", "entra no site", "navega", "cadastr")
_SEARCH_HINTS = ("pesquis", "procur", "busc")
_NAV_VERBS = ("abre", "abrir", "abra", "vai para", "vai pra", "acessa", "acessar", "entra em", "entra no", "entra na",
              "navega para", "abre o site", "abre a pagina", "abre a página")


def _norm(text: str) -> str:
    v = str(text or "").lower().strip()
    v = unicodedata.normalize("NFD", v)
    v = "".join(ch for ch in v if unicodedata.category(ch) != "Mn")
    v = re.sub(r"\s+", " ", v).strip()
    return re.sub(r"^(ei |hey |ok |kira[, ]+|chefe[, ]+)+", "", v)


_VOICE_AT_RE = re.compile(r"\s*\b(?:arroba|at)\b\s*", re.IGNORECASE)


def normalize_voice_email_text(raw: str) -> str:
    """FASE 4B — repara e-mail ditado por voz (STT) antes da extracao.

    'teste arroba example ponto com' -> 'teste@example.com'.
    So atua quando ha 'arroba'/'ponto com/net/org/br' E o texto ainda nao tem '@'
    textual, para nunca danificar pontuacao normal de uma frase comum.
    """
    s = str(raw or "")
    if "@" in s:
        return s  # ja veio com e-mail textual
    low = s.lower()
    if "arroba" not in low and not re.search(r"\bponto\s+(com|net|org|io|dev|br)\b", low):
        return s
    out = _VOICE_AT_RE.sub("@", s)
    out = re.sub(r"\s*\bponto\s+com\s+br\b", ".com.br", out, flags=re.IGNORECASE)
    out = re.sub(r"\s*\bponto\s+(com|net|org|io|dev|br)\b", r".\1", out, flags=re.IGNORECASE)
    out = re.sub(r"\bponto\b", ".", out, flags=re.IGNORECASE)
    # junta espacos so quando cercado por chars de palavra (o proprio e-mail)
    out = re.sub(r"(\w)\s*@\s*(\w)", r"\1@\2", out)
    out = re.sub(r"(\w)\s*\.\s*(\w)", r"\1.\2", out)
    return out


_PAUSE_PHRASES = (
    "pausa isso", "pausa ai", "pausa", "pausar", "para ai", "para um pouco",
    "espera que eu volto", "espera ai que eu volto", "espera ai", "espera um pouco",
    "depois a gente continua", "para de fazer por agora", "segura ai", "da um tempo",
)
_RESUME_PHRASES = (
    "pode continuar", "continua", "continuar", "volta la", "volta a fazer",
    "termina o que tava fazendo", "termina o que estava fazendo", "retoma", "retomar",
    "volta pro que tava fazendo", "pode voltar",
)


def _is_pause_phrase(n: str) -> bool:
    return any(p == n or p in n for p in _PAUSE_PHRASES)


def _is_resume_phrase(n: str) -> bool:
    return any(p == n or p in n for p in _RESUME_PHRASES)


def _extract_url(raw: str) -> tuple[str, str]:
    """Retorna (url, target_service)."""
    n = _norm(raw)
    # file:// (usado em testes/fixtures locais)
    fm = re.search(r"(file://\S+)", raw)
    if fm:
        return fm.group(1), ""
    for name, url in _KNOWN_SITES.items():
        if name == "x" and (
            re.search(r"\d\s*x\s*\d", n)
            or not re.search(r"\b(o|no|do|abre|abrir|abra|acessa|acessar|entra|entrar)\s+x\b", n)
        ):
            # "x" sozinho só é o site com contexto ("abre o x", "no x"); "7 x 8" é conta.
            continue
        if re.search(rf"\b{name}\b", n):
            return url, name
    # remove emails antes de procurar URL (evita example.com de um email virar site)
    raw_no_email = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", " ", raw)
    m = _URL_RE.search(raw_no_email)
    if m:
        u = m.group(1)
        if not u.lower().startswith("http"):
            u = "https://" + u
        return u, ""
    return "", ""


_YT_MEDIA_RE = re.compile(
    r"\b(coloca|toca|bota|p[oõ]e|poe|ouve|ouvir|escuta|pesquisa|pesquisar|procura|busca|abre|abrir|play|assiste|assistir|ve|ver|reproduz)\b"
    r".{0,60}\b(you\s*tube|youtube|yt)\b",
    re.IGNORECASE,
)


def is_youtube_command(text: str) -> bool:
    """SINCRONO, regex puro, zero IO/rede: True se o texto e um comando de midia no
    YouTube (coloca/toca/bota/poe/ouve/pesquisa/abre/play ... youtube). Usado pelo
    caminho de voz para decidir matched=True na hora, sem nenhum await/classificador."""
    return bool(_YT_MEDIA_RE.search(str(text or "")))


def detect_browser_agent_command(text: str) -> BrowserAgentCommand:
    raw = str(text or "")
    n = _norm(raw)
    if not n:
        return BrowserAgentCommand(False)
    if any(neg in n for neg in _NEGATIVE):
        return BrowserAgentCommand(False)
    # Frases de continuacao/segredo NAO sao um novo comando de browser
    # (sao tratadas pelo follow-up gate quando ha job ativo).
    if re.search(r"\b(a senha|minha senha|usa esse email|usa o email|o email e|meu email e|salva no cofre|usa so agora)\b", n):
        return BrowserAgentCommand(False)
    # Email solto (sem verbo de navegacao explicito) nao e browser.
    if re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", raw) and not re.search(r"\b(abre|abrir|abra|vai|acessa|entra|navega|pesquis|procur|busc|site|navegador)\b", n):
        return BrowserAgentCommand(False)
    # criar arquivo/pdf nao e browser
    if re.search(r"\b(cria|criar|gera|gerar|monta)\b.*\b(pdf|arquivo|texto|documento|planilha|slide)\b", n):
        return BrowserAgentCommand(False)

    url, service = _extract_url(raw)
    is_search = any(h in n for h in _SEARCH_HINTS)
    is_multistep = any(h in n for h in _MULTISTEP_HINTS)
    goal_intent, auth_allowed = _classify_goal_intent(n)
    logger.info("BROWSER_GOAL_INTENT_DETECTED intent=%s auth_allowed=%s", goal_intent, str(auth_allowed).lower())
    has_nav_verb = any(re.search(rf"\b{re.escape(v)}\b", n) for v in _NAV_VERBS) or n.startswith("abre") or n.startswith("vai")
    mentions_web = bool(url) or "internet" in n or "site" in n or "pagina" in n or "navegador" in n

    # YouTube (e sites de midia): "coloca/toca/bota/poe/ouve/ve/abre/pesquisa X no youtube"
    # -> SEMPRE busca no YouTube com a query extraida (action=youtube_search), NUNCA
    # navigate puro (que perdia a musica e abria so a home -> virava 'form' e morria).
    # Vem ANTES da busca generica e do navigate. So dispara quando ha uma query real;
    # "abre o youtube" (sem termo) cai no navigate normal abaixo.
    if service == "youtube":
        yq = _extract_media_query(n)
        if yq:
            return BrowserAgentCommand(
                True, action="youtube_search", browser_goal=f"coloca {yq} no youtube",
                query=yq, requires_multistep=False, target_service="youtube", confidence=0.92,
                text_only_ack="Pode deixar, chefe. Vou colocar no YouTube.",
                goal_intent="youtube_search", auth_allowed=False,
            )

    # Pesquisa
    if is_search and ("google" in n or "internet" in n or "bing" in n or "sobre" in n or mentions_web):
        query = _extract_query(n)
        return BrowserAgentCommand(
            True, action="search", browser_goal=f"pesquisar: {query}", query=query,
            requires_multistep=False, target_service=service, confidence=0.9,
            text_only_ack="Beleza, chefe. Vou pesquisar isso no navegador.",
            goal_intent="search", auth_allowed=False,
        )

    # Multi-step (observar/login/form/explorar)
    if (is_multistep or goal_intent in {"login_assisted", "observe_page", "fill_form"}) \
            and (mentions_web or url or "site" in n or "pagina" in n or "conta" in n):
        ack = ("Certo, chefe. Vou abrir e te dizer o que aparece." if goal_intent == "observe_page"
               else "Certo, chefe. Vou abrir no navegador e trabalhar nisso.")
        return BrowserAgentCommand(
            True, action="multistep", browser_goal=raw.strip(), url=url,
            requires_multistep=True, target_service=service, confidence=0.9,
            text_only_ack=ack, goal_intent=goal_intent, auth_allowed=auth_allowed,
        )

    # Navegacao simples. Site conhecido citado sem pedido ("mexendo no Instagram", contando o dia) nao abre nada:
    # so com verbo de navegar; endereco digitado ("github.com/x") abre direto.
    if (has_nav_verb and mentions_web) or (url and not service):
        return BrowserAgentCommand(
            True, action="navigate", browser_goal=f"abrir {url or service or 'site'}", url=url,
            requires_multistep=False, target_service=service, confidence=0.85,
            text_only_ack="Pode deixar, chefe. Abrindo no navegador.",
            goal_intent="navigate", auth_allowed=False,
        )

    return BrowserAgentCommand(False)


# ── FASE 1 — detectores do fluxo por contexto (navegador embutido / aba Internet) ──
def is_navigation_command(text: str) -> bool:
    """Browser FECHADO: True so com intencao EXPLICITA de navegar (site/youtube/google,
    'abre/pesquisa no', URL, comando de midia). Reusa a deteccao ja existente."""
    return bool(detect_browser_agent_command(text).matched)


_CONTROL_RE = re.compile(
    r"\b(clica|clique|clicar|aperta|aperte|apertar|toca (?:em|no|na|ali)|seleciona|selecionar|escolhe|escolher|"
    r"primeiro|segundo|terceiro|quarto|quinto|"
    r"desce|descer|sobe|subir|rola|rolar|"
    r"volta|voltar|proxim[oa]|anterior|"
    r"pausa|pausar|para|parar|continua|continuar|play|"
    r"fecha|fechar|tela cheia|legenda|mut[ae]|mutar|"
    r"inscreve|inscrever|curte|curtir|like|dislike|"
    r"comenta|comentar|comentarios|pula|pular)\b"
)


def is_control_command(text: str) -> bool:
    """Browser ABERTO: threshold BAIXO — qualquer comando de controle/escolha conta
    (clica/desce/sobe/volta/proximo/pausa/o segundo/inscreve/like/comenta/fecha...).
    So deve ser consultado quando a aba Internet esta aberta (contexto ja ativo)."""
    n = _norm(text)
    if not n:
        return False
    if _CONTROL_RE.search(n):
        return True
    # posicao numerica isolada ("o 2", "o 3") tambem conta como escolha
    if re.search(r"\bo\s+\d+\b", n):
        return True
    return False


_OPEN_TARGET_LOCAL_RE = re.compile(
    r"\b(no meu navegador|meu navegador|no meu chrome|no chrome|no edge|no firefox|"
    r"navegador do windows|navegador externo|no meu pc|no meu computador|navegador do pc)\b"
)
_OPEN_TARGET_EMBEDDED_RE = re.compile(
    r"\b(aqui|aqui mesmo|aqui na kira|no seu navegador|no teu navegador|na aba|na sua aba|"
    r"no navegador da kira|no navegador interno|internamente|dentro da kira|na sua tela)\b"
)


def resolve_open_target(text: str) -> str:
    """Alvo do 'abrir': 'embedded' (aba/navegador da Kira) | 'local' (Chrome/Edge/
    Firefox do Windows). DEFAULT = embedded: a aba Internet é o navegador padrão da Kira;
    só vai pro navegador local quando a fala for EXPLÍCITA ('no meu navegador/chrome/edge')."""
    n = _norm(text)
    if _OPEN_TARGET_LOCAL_RE.search(n):
        return "local"
    return "embedded"


def youtube_query_of(text: str) -> "str | None":
    """FASE 2 — se o comando for de YouTube COM termo de busca, retorna a query (pro
    dispatcher abrir via memória/busca no YouTube); senão None ('abre o youtube' sem termo)."""
    cmd = detect_browser_agent_command(text)
    if cmd.action == "youtube_search" and cmd.query:
        return cmd.query
    return None


def build_open_url(text: str) -> str:
    """FASE 1 — monta a URL direta a partir do pedido (sem navegar pra home e clicar):
    YouTube -> results?search_query=...; site nomeado/URL -> direto; busca -> Google.
    Reusa a deteccao ja existente. Vazio se nao deu pra resolver (caller usa fallback)."""
    import urllib.parse as _u
    cmd = detect_browser_agent_command(text)
    if cmd.action == "youtube_search" and cmd.query:
        return "https://www.youtube.com/results?search_query=" + _u.quote_plus(cmd.query)
    if cmd.url:
        return cmd.url
    if cmd.action == "search" and cmd.query:
        return "https://www.google.com/search?q=" + _u.quote_plus(cmd.query)
    return ""


# Intencao de autenticacao SO quando o usuario pede login/entrar explicitamente.
_LOGIN_INTENT_HINTS = ("faz login", "fazer login", "tenta fazer login", "faz o login", "login nesse", "login no site",
                       "tenta entrar", "entra na minha conta", "entrar na conta", "entra na conta",
                       "acessa minha conta", "acessar minha conta", "loga ", "logar", "logue")  # "minha conta" sozinho pegava "minha conta tava bloqueada"
_OBSERVE_INTENT_HINTS = ("me diz o que aparece", "do que se trata", "do que ele trata", "me fala o que aparece",
                         "me diz o que esse", "me diz o que e", "resume", "resumir", "analisa", "o que esse site",
                         "ve do que", "ve o que aparece", "me mostra o que tem", "o que aparece", "o que tem nessa")
_FORM_INTENT_HINTS = ("preench", "formulario", "formulário", "cadastr")


def _classify_goal_intent(n: str) -> tuple[str, bool]:
    """Retorna (goal_intent, auth_allowed). auth so com pedido explicito de login."""
    if any(h in n for h in _LOGIN_INTENT_HINTS):
        return "login_assisted", True
    if any(h in n for h in _OBSERVE_INTENT_HINTS):
        return "observe_page", False
    if any(h in n for h in _FORM_INTENT_HINTS):
        return "fill_form", False
    return "unknown", False


_MEDIA_VERBS_RE = re.compile(
    r"\b(coloca|bota|boto|toca|tocar|poe|poem|reproduz(?:ir)?|assist(?:e|ir)|ouv(?:ir|e)|"
    r"escut(?:a|ar)|ve|ver|abre|abrir|abra|pesquis\w*|procur\w*|busc\w*)\b"
)


def _strip_target_words(g: str) -> str:
    """Remove as palavras de ALVO ('aqui', 'no seu/meu navegador', 'na aba', 'no chrome'...)
    da query: elas indicam ONDE abrir, não O QUE pesquisar. Sem isso, "abre o youtube aqui"
    virava busca por 'aqui'. `g` já vem normalizado (minúsculo, sem acento)."""
    g = _OPEN_TARGET_LOCAL_RE.sub(" ", g)
    g = _OPEN_TARGET_EMBEDDED_RE.sub(" ", g)
    return g


def _extract_media_query(n: str) -> str:
    """Extrai a query de um comando de YouTube/midia: remove o verbo de midia e a
    referencia ao 'youtube'/'yt' e palavras de classe (musica/video/clipe/...).
    `n` ja vem normalizado (minusculo, sem acento). Vazio = sem termo real."""
    g = _strip_target_words(n)  # tira 'aqui'/'no seu navegador'/... ANTES de extrair
    g = re.sub(r"\b(no|na|do|da|pelo|pela)?\s*you\s*tube\b", " ", g)
    g = re.sub(r"\byt\b", " ", g)
    g = _MEDIA_VERBS_RE.sub(" ", g)
    g = re.sub(r"\b(ultim[oa]|primeir[oa]|video|videos|musica|clipe|cancao|live|playlist|canal)\b", " ", g)
    g = re.sub(r"^\s*(a|o|os|as|um|uma|me|pra|para|de|do|da)\b", " ", g)
    g = re.sub(r"\s+", " ", g).strip(" .,!?\"'")
    return g


def _extract_query(n: str) -> str:
    n = _strip_target_words(n)  # 'aqui'/'no meu navegador'/... não fazem parte da busca
    m = re.search(r"\b(?:pesquis\w*|procur\w*|busc\w*)\b\s*(?:no google|na internet|no bing|sobre|por|:)?\s*(.+)$", n)
    if m:
        q = m.group(1).strip(" .:-")
        q = re.sub(r"^(no google|na internet|no bing|sobre|por)\s+", "", q).strip()
        return q
    return n


# --------------------------------------------------------------------------
# continuacao de job ativo
# --------------------------------------------------------------------------
# Sinais de "nova tarefa" embutidos numa frase (nao confirmam job pendente).
_NEW_TASK_SIGNALS = (
    "preench", "formulario", "abre", "abrir", "acessa", "entra", "site", "http", "www",
    "login", "logar", "whatsapp", " zap", "mensagem", "pesquis", "procur", "busc",
    "cria", "gera", "navega", "cadastr", "responde e", "manda mensagem",
)
_CONFIRM_RE = re.compile(
    r"\b(sim|pode|pode sim|pode ir|pode enviar|pode mandar|pode confirmar|envia agora|envia|enviar|"
    r"confirmo|confirma|confirmar|manda|mandar|manda sim|tenta|tentar|ok|okay|beleza|blz|isso|isso mesmo)\b"
)
_DECLINE_RE = re.compile(
    r"\b(nao envia|nao manda|nao confirma|nao confirmar|nao pode|cancela|cancelar|nega|negativo|nao quero)\b"
)


def is_new_task_like_text(text: str) -> bool:
    n = _norm(text)
    if any(sig in n for sig in _NEW_TASK_SIGNALS):
        return True
    # "... para <alguem>" junto de envio sugere nova tarefa (ex.: envia para Amye)
    if re.search(r"\b(envia|enviar|manda|mandar)\b.*\bpar[ao]\s+\w+", n):
        return True
    return False


def is_explicit_browser_confirmation(text: str) -> bool:
    """Confirmacao SO se for curta, isolada e sem cara de nova tarefa."""
    n = _norm(text)
    if is_new_task_like_text(text):
        return False
    if len(n.split()) > 5:
        return False
    return bool(_CONFIRM_RE.search(n))


def is_explicit_browser_decline(text: str) -> bool:
    n = _norm(text)
    if len(n.split()) > 5:
        return False
    if n in {"nao", "cancela", "cancelar", "nao envia"}:
        return True
    return bool(_DECLINE_RE.search(n))


_WINDOW_SHOW_PHRASES = (
    "me mostra o navegador", "mostra o navegador", "abre o navegador na minha tela",
    "traz o navegador", "poe o navegador na tela", "poe na tela", "coloca na minha tela",
    "abre na minha tela", "mostra na minha tela", "abre a janela do navegador",
)
_WINDOW_PARK_PHRASES = (
    "esconde o navegador", "esconde a janela", "pode esconder", "manda pra fora da tela",
    "tira da minha frente", "tira da frente", "deixa rodando escondido", "joga pro canto",
    "esconde isso", "some com a janela", "manda pro canto", "esconde",
)


def _is_window_show_phrase(n: str) -> bool:
    return any(p in n for p in _WINDOW_SHOW_PHRASES)


def _is_window_park_phrase(n: str) -> bool:
    return any(p in n for p in _WINDOW_PARK_PHRASES)


def detect_window_command(text: str) -> str:
    """Comando puro de janela (sem precisar de job): 'show' | 'park' | ''."""
    n = _norm(text)
    if not n:
        return ""
    if _is_window_park_phrase(n):
        return "park"
    if _is_window_show_phrase(n):
        return "show"
    return ""


# ── FASE 3.2D-HOTFIX: redacao de senha na transcricao/UI/logs ─────────────────
# Flag em memoria: True quando ha um job de browser aguardando segredo (senha).
# Setado pelo handler de follow-up; usado para redigir o turno do usuario.
_BROWSER_SECRET_EXPECTED = False

# Padroes que indicam credencial no texto exibido.
_SECRET_KEYWORD_RE = re.compile(
    r"(?i)\b(senha|password|secret|pin)\b(\s*(?:é|eh|e|:|=|do\s+site|da\s+conta|sera|será)?\s*)(\S+)"
)
_EMAIL_DISPLAY_RE = re.compile(r"([\w.+-])[\w.+-]*(@[\w.-]+\.[\w.-]+)")
_NORMAL_QUESTION_HINTS = (
    "quem ", "qual ", "quanto", "que horas", "como ", "porque", "por que",
    "onde ", "quando ", "?", "abre ", "pesquisa", "cancela", "para ",
)


def set_browser_secret_expected(value: bool) -> None:
    global _BROWSER_SECRET_EXPECTED
    _BROWSER_SECRET_EXPECTED = bool(value)


def is_browser_secret_expected() -> bool:
    return _BROWSER_SECRET_EXPECTED


def redact_browser_sensitive_text(text: str, *, secret_expected: bool | None = None) -> str:
    """Redige senha/credencial APENAS para exibicao/log. NUNCA altera o valor raw
    usado internamente para enviar ao Browser Agent. Mascara e-mail parcialmente.
    Se `secret_expected` (job aguardando senha) e o texto nao parece pergunta normal,
    oculta o turno inteiro (senha digitada/falada sozinha)."""
    s = str(text or "")
    if not s:
        return s
    se = _BROWSER_SECRET_EXPECTED if secret_expected is None else bool(secret_expected)
    has_keyword = bool(re.search(r"(?i)\b(senha|password|secret|pin)\b", s))
    # 1) valor apos 'senha/password/...' -> [SENHA OCULTA]
    s2 = _SECRET_KEYWORD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[SENHA OCULTA]", s)
    # 2) e-mail parcial
    s2 = _EMAIL_DISPLAY_RE.sub(lambda m: f"{m.group(1)}***{m.group(2)}", s2)
    # 3) senha CRUA quando o job aguarda segredo e nao e pergunta normal
    if se and not has_keyword:
        low = s.lower()
        looks_normal = any(h in low for h in _NORMAL_QUESTION_HINTS)
        has_email = bool(_EMAIL_DISPLAY_RE.search(s))
        # turno curto sem cara de pergunta e sem e-mail = provavel senha isolada
        if not looks_normal and not has_email and len(s.split()) <= 4:
            return "[CREDENCIAL ENVIADA AO NAVEGADOR — SENHA OCULTA]"
    return s2


def detect_browser_followup(text: str, *, need_type: str) -> BrowserFollowup:
    """Detecta resposta do usuario para o `need_type` ativo. need_type vazio =
    sem job esperando -> so reconhece 'cancela'/'mostra janela'."""
    raw = str(text or "")
    n = _norm(raw)
    if not n:
        return BrowserFollowup(False)

    # FASE 1 (agente YouTube): video tocando em aguardando_comando -> os controles
    # (pausa/pular/legenda/inscrever/like/comentar/fechar...) tem PRIORIDADE e sao
    # tratados ANTES dos gestos genericos de pause/resume/park do fluxo de browser.
    if need_type == "needs_youtube_command":
        if _is_window_park_phrase(n):
            return BrowserFollowup(True, kind="park")
        return _parse_youtube_control(n, raw)

    # cancelar (qualquer estado)
    if re.search(r"\bcancela(r)?\b.*\b(tarefa|navegador|browser|isso)\b", n) or n in {"cancela", "cancelar"} \
            or "para de fazer isso" in n or "nao precisa mais" in n or "fecha isso ai" in n:
        return BrowserFollowup(True, kind="cancel")

    # FASE 4B — pausar / retomar (qualquer estado, antes de park)
    if _is_pause_phrase(n):
        return BrowserFollowup(True, kind="pause")
    # Durante entrada manual (needs_user_action), "pode continuar"/"continua" significa
    # "terminei de digitar" -> trata como conclusao (user_action_done), nao como resume.
    if _is_resume_phrase(n) and need_type != "needs_user_action":
        return BrowserFollowup(True, kind="resume")

    # estacionar/esconder janela (qualquer estado) — NAO fecha o navegador
    if _is_window_park_phrase(n):
        return BrowserFollowup(True, kind="park")

    manual_show_requested = bool(
        re.search(r"\b(mostra|mostrar|abre|abrir|exibe)\b.*\b(janela|tela|navegador)\b", n)
        or re.search(r"(me mostra|mostra) que eu (coloco|coloca|preencho|preenche|digito|escrevo)", n)
        or re.search(r"deixa eu (preencher|colocar|digitar|escrever)", n)
        or re.search(r"deixa que eu (preencho|coloco|digito|escrevo|faco)", n)
        or re.search(r"vou (colocar|preencher|digitar|escrever)\s*(manualmente|na janela|direto|aqui)?", n)
        or re.search(r"\beu (preencho|coloco|digito)\b", n)
        or re.search(r"\b(prefiro|melhor|quero|vou)\s+(digitar|colocar|preencher|manual)\b", n)
        or re.search(r"\bmanual(mente)?\b", n)
        or "coloco la" in n or "coloca la" in n
        or "que eu resolvo" in n or "eu resolvo" in n
        or "deixa eu digitar" in n or "vou digitar manualmente" in n
        or "eu digito" in n or "prefiro manual" in n or "me mostra" in n
        or "abre a janela" in n or "mostra a janela" in n
    )
    manual_done = bool(re.fullmatch(
        r"(ja\s+)?(resolvi|resolvido|pronto|prontinho|terminei|terminado|finalizei|feito|consegui|"
        r"digitei|coloquei|preenchi|escrevi)|pode\s+continuar|continua(r)?", n))

    # Nova pergunta separada: "quer salvar no cofre?" (apos credencial por voz/texto).
    if need_type == "needs_save_decision":
        if re.search(r"\b(nao|não|deixa|nem precisa|nao precisa|negativo|nunca|so agora|só agora)\b", n):
            logger.info("BROWSER_SAVE_DECISION_NO")
            return BrowserFollowup(True, kind="save_no")
        if re.search(r"\b(sim|pode|salva|salvar|guarda|guardar|claro|quero|manda|positivo|ok|okay|beleza|blz|isso|por favor)\b", n):
            logger.info("BROWSER_SAVE_DECISION_YES")
            return BrowserFollowup(True, kind="save_yes")
        return BrowserFollowup(False)

    if need_type == "needs_youtube_choice":
        # ETAPA 4 — rolagem da lista de resultados
        if re.search(r"\b(desce|descer|rola pra baixo|mais pra baixo|mostra mais|proximos|ver mais|abaixo)\b", n):
            logger.info("BROWSER_YOUTUBE_FOLLOWUP kind=scroll_down")
            return BrowserFollowup(True, kind="youtube_scroll", value="down")
        if re.search(r"\b(sobe|subir|rola pra cima|volta pra cima|mais pra cima|anteriores|acima)\b", n):
            logger.info("BROWSER_YOUTUBE_FOLLOWUP kind=scroll_up")
            return BrowserFollowup(True, kind="youtube_scroll", value="up")
        # qualquer outra fala = escolha (posicao OU titulo); o agente interpreta.
        raw_choice = str(text or "").strip()
        if raw_choice:
            logger.info("BROWSER_YOUTUBE_FOLLOWUP kind=choice")
            return BrowserFollowup(True, kind="youtube_choice", value=raw_choice)
        return BrowserFollowup(False)

    if need_type == "needs_user_action":
        if manual_show_requested:
            return BrowserFollowup(True, kind="show")
        if manual_done:
            return BrowserFollowup(True, kind="user_action_done")
        return BrowserFollowup(False)

    if need_type == "needs_confirmation":
        logger.info("BROWSER_CONFIRMATION_CHECK text_words=%d", len(n.split()))
        # 1) decline curto e claro
        if is_explicit_browser_decline(raw):
            logger.info("BROWSER_CONFIRMATION_DECLINED")
            return BrowserFollowup(True, kind="decline")
        # 2) frase com cara de NOVA tarefa -> nao confirma o job pendente
        if is_new_task_like_text(raw):
            logger.info("BROWSER_CONFIRMATION_REJECTED_NEW_TASK_LIKE")
            return BrowserFollowup(False)
        # 3) confirmacao explicita, curta e isolada
        if is_explicit_browser_confirmation(raw):
            logger.info("BROWSER_CONFIRMATION_ACCEPTED")
            return BrowserFollowup(True, kind="confirm")
        # 4) tem 'envia/confirma' mas e ambiguo -> pedir esclarecimento, manter pendente
        if re.search(r"\b(envia|enviar|confirma|confirmo|manda|mandar)\b", n):
            logger.info("BROWSER_CONFIRMATION_AMBIGUOUS")
            logger.info("BROWSER_CONFIRMATION_LEFT_PENDING")
            return BrowserFollowup(True, kind="ambiguous")
        return BrowserFollowup(False)

    if need_type == "needs_secret":
        if manual_show_requested:
            return BrowserFollowup(True, kind="show")
        if manual_done:
            return BrowserFollowup(True, kind="user_action_done")
        no_save = bool(re.search(r"(nao salva|não salva|so agora|só agora|nao guarda|usa so agora|usa só agora)", n))
        save = (not no_save) and bool(re.search(r"\b(salva|salvar|pode salvar|guarda|guardar)\b", n) or "salva no cofre" in n)
        value = _extract_secret_value(raw)
        if value:
            return BrowserFollowup(True, kind="secret", value=value, save=save)
        # instrucao isolada de salvar/nao salvar (segredo ja enviado)
        if save:
            return BrowserFollowup(True, kind="save", save=True)
        if no_save:
            return BrowserFollowup(True, kind="no_save", save=False)
        # mencionou senha mas nao deu pra extrair -> pede de forma clara (sem 400)
        if re.search(r"\b(senha|password|secret)\b", n):
            return BrowserFollowup(True, kind="secret_parse_failed")
        # FASE 3.2D: senha CRUA — token unico isolado (digitado sozinho), descartando
        # palavras de controle. Preserva case. Nunca casa perguntas (tem espacos).
        bare = str(raw or "").strip()
        if bare and " " not in bare and len(bare) >= 4:
            _control = {"sim", "nao", "pode", "cancela", "cancelar", "mostra", "mostrar",
                        "ok", "okay", "beleza", "salva", "salvar", "guarda", "guardar",
                        "tenta", "tentar", "deixa", "para"}
            cleaned = _clean_secret_token(bare)
            if cleaned and _norm(bare) not in _control:
                return BrowserFollowup(True, kind="secret", value=cleaned, save=save)
        return BrowserFollowup(False)

    if need_type == "needs_input":
        if manual_show_requested:
            return BrowserFollowup(True, kind="show")
        if manual_done:
            return BrowserFollowup(True, kind="user_action_done")
        # FASE 4B: repara e-mail ditado por voz (arroba/ponto com) antes de extrair.
        raw = normalize_voice_email_text(raw)
        # FASE 3.2D-HOTFIX: combo "email X e senha Y" no mesmo turno (waiting_input).
        combo = _extract_email_and_secret(raw)
        if combo is not None:
            email_v, secret_v = combo
            return BrowserFollowup(True, kind="input_with_secret", value=email_v, secret=secret_v)
        value = _extract_input_value(raw)
        if value:
            return BrowserFollowup(True, kind="input", value=value)
        # mencionou e-mail/usuario/coloca mas nao deu pra extrair -> pede claro (sem Gemini)
        if re.search(r"\b(e-?mail|usuario|usuário|login|coloca|usa esse|usa o)\b", n):
            return BrowserFollowup(True, kind="input_parse_failed")
        return BrowserFollowup(False)

    return BrowserFollowup(False)


def _extract_email_and_secret(raw: str):
    """FASE 3.2D-HOTFIX: extrai (email, senha) quando vierem juntos no mesmo turno.
    Exige e-mail valido + palavra-chave 'senha/password' seguida do token. Retorna
    None se nao houver os dois (evita falso positivo em perguntas normais)."""
    s = str(raw or "")
    m_email = re.search(r"([\w.+-]+@[\w-]+\.[\w.-]+)", s)
    if not m_email:
        return None
    m_sec = re.search(
        r"(?i)\b(?:senha|password|secret|pin)\b\s*(?:é|eh|e|:|=|do\s+site|da\s+conta|sera|será)?\s*(\S+)",
        s,
    )
    if not m_sec:
        return None
    secret = _clean_secret_token(m_sec.group(1))
    if secret:
        return (m_email.group(1), secret)
    return None


def _clean_secret_token(tok: str) -> str:
    t = str(tok or "").strip().strip(".,:;\"'()? ")
    # nunca tratar palavras de instrucao como senha
    if t.lower() in {"so", "só", "a", "o", "agora", "senha", "minha", "essa", "esse", "esta", "favor"}:
        return ""
    return t


def _extract_secret_value(raw: str) -> str:
    """A senha e SEMPRE um unico token. Captura so o token apos o marcador."""
    n = str(raw or "").strip()
    # padrao 1: "senha [é|e|:|=] <token>"
    m = re.search(r"\bsenha\b\s*(?:e|eh|é|sera|será|do site|da conta)?\s*[:=]?\s*(\S+)", n, re.IGNORECASE)
    if m:
        v = _clean_secret_token(m.group(1))
        if v:
            return v
    # padrao 2: "usa[r] [a senha] <token> [so agora]"
    m = re.search(r"\b(?:usa|use|usar)\s+(?:a\s+senha\s+)?(\S+)", n, re.IGNORECASE)
    if m:
        v = _clean_secret_token(m.group(1))
        if v:
            return v
    return ""


def _extract_input_value(raw: str) -> str:
    n = str(raw or "").strip()
    m = re.search(r"\b(?:usa|use|usar)\s+(?:esse|este|o|meu|o meu)?\s*(?:e-?mail|email|usuario|usuário|login|termo)?\s*[:=]?\s*(\S.+)$", n, re.IGNORECASE)
    if m:
        return m.group(1).strip(".\"' ")
    m = re.search(r"\b(?:e-?mail|email|usuario|usuário|login|termo|pesquisa)\s+(?:e|eh|é)?\s*[:=]?\s*(\S.+)$", n, re.IGNORECASE)
    if m:
        return m.group(1).strip(".\"' ")
    # email solto
    m = re.search(r"([\w.+-]+@[\w-]+\.[\w.-]+)", n)
    if m:
        return m.group(1)
    return ""


# ── FASE 1 (agente YouTube): parser dos controles do video em aguardando_comando ──
_YT_NUM_RE = re.compile(r"(\d+)")


def _yt_secs(n: str, default: str = "10") -> str:
    m = _YT_NUM_RE.search(n)
    secs = m.group(1) if m else default
    if "minuto" in n and secs.isdigit():
        secs = str(int(secs) * 60)
    return secs


def _parse_youtube_control(n: str, raw: str) -> BrowserFollowup:
    """Mapeia a fala do usuario (video tocando) para um comando de controle.
    `n` ja normalizado (minusculo, sem acento); `raw` preserva texto p/ comentario."""
    YC = lambda cmd, val="": BrowserFollowup(True, kind="youtube_control", command=cmd, value=val)  # noqa: E731

    # fechar o navegador
    if re.search(r"\b(fecha|fechar|encerra|encerrar)\b.*\b(navegador|browser|youtube|video|isso|tudo)\b", n) \
            or n in {"fecha", "fechar", "fecha isso", "pode fechar"}:
        return YC("close")
    # comentar <texto> (preserva o texto original do raw)
    if re.search(r"\b(comenta|comentar|comente)\b", n) or "escreve um comentario" in n or "escrever um comentario" in n:
        mraw = re.search(r"(?i)\b(?:comenta(?:r)?|comente|escrev[a-z]*\s+um\s+comentario(?:\s+dizendo)?)\b[:\s]*(.*)$", str(raw or ""))
        txt = (mraw.group(1).strip(" .:-\"'") if mraw and mraw.group(1).strip() else "")
        return YC("comment", txt)
    # ler comentarios
    if "comentari" in n and re.search(r"\b(le|ler|leia|fala|diz|quais|mostra|me da)\b", n):
        return YC("read_comments")
    # inscrever / cancelar inscricao / notificacoes
    if re.search(r"\b(cancela|cancelar|tira|remove|desinscreve|desinscrever)\b", n) and "inscri" in n:
        return YC("unsubscribe")
    if re.search(r"\b(se inscreve|se inscrever|inscreve|inscrever|inscreva)\b", n):
        return YC("subscribe")
    if re.search(r"\b(ativa|liga|coloca)\b.*\b(notifica|sino)\b", n) or re.search(r"\b(notificacoes|notificacao|sininho|sino)\b", n):
        return YC("notifications")
    # like / dislike
    if re.search(r"\b(dislike|descurte|descurtir|nao curti|nao gostei)\b", n) or "joinha pra baixo" in n:
        return YC("dislike")
    if re.search(r"\b(da like|deixa o like|curte|curtir|gostei|da um joinha|joinha|like)\b", n):
        return YC("like")
    # seek (avancar/retroceder N segundos)
    if re.search(r"\b(pula|pular|avanca|avancar|adianta|adiantar|pula pra frente|pra frente)\b", n) \
            and ("segundo" in n or "minuto" in n or _YT_NUM_RE.search(n)) and "proxim" not in n:
        return YC("seek_forward", _yt_secs(n))
    if re.search(r"\b(volta|voltar|retrocede|retroceder|pra tras)\b", n) \
            and ("segundo" in n or "minuto" in n or "um pouco" in n or _YT_NUM_RE.search(n)) \
            and not re.search(r"\b(video|anterior)\b", n):
        return YC("seek_back", _yt_secs(n))
    # proximo / anterior
    if re.search(r"\b(proximo|proxima|pula esse|pula o video|proximo video|pular video|pula essa)\b", n):
        return YC("next")
    if re.search(r"\b(anterior|video anterior|volta o video|volta pro anterior|volta pra anterior)\b", n):
        return YC("previous")
    # do comeco
    if re.search(r"\b(do comeco|desde o comeco|do inicio|reinicia|recomeca|comeca de novo|comecar de novo)\b", n):
        return YC("from_start")
    # legenda / tela cheia / mute
    if re.search(r"\b(legenda|legendas|caption|cc)\b", n):
        return YC("captions")
    if re.search(r"\b(tela cheia|tela inteira|fullscreen|maximiza)\b", n):
        return YC("fullscreen")
    if re.search(r"\b(muta|mutar|sem som|silencia|tira o som|bota o som|coloca o som)\b", n):
        return YC("mute")
    # scroll
    if re.search(r"\b(desce|descer|rola pra baixo|mais pra baixo|abaixo)\b", n):
        return YC("scroll_down")
    if re.search(r"\b(sobe|subir|rola pra cima|mais pra cima|acima)\b", n):
        return YC("scroll_up")
    # pause / play (curtos, por ultimo)
    if re.search(r"\b(pausa|pausar|para|parar|congela|segura)\b", n):
        return YC("pause")
    if re.search(r"\b(continua|continuar|play|da play|toca de novo|volta a tocar|retoma|bota pra tocar)\b", n):
        return YC("play")
    return BrowserFollowup(False)
