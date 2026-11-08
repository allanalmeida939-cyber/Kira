from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata

from .personality_response import format_manus_task_ack

logger = logging.getLogger(__name__)


@dataclass
class CreationTaskCommand:
    task_type: str
    raw_text: str
    normalized_text: str
    confidence: float
    requested_output: str
    title_hint: str
    should_collect_more_details: bool
    destination_mode: str = "ask_later"
    destination_hint: str = ""
    has_image: bool = False


_REPLACEMENTS = {
    "pdfi": "pdf",
    "p d f": "pdf",
    "pd f": "pdf",
    "power point": "powerpoint",
    "pawer point": "powerpoint",
    "powerpointe": "powerpoint",
    "slayde": "slide",
    "slaide": "slide",
    "eslaide": "slide",
    "prompts": "prompt",
    "piton": "python",
    "paithon": "python",
    "java script": "javascript",
    "c sharp": "csharp",
    "h t m l": "html",
}
_WAKE_PREFIX = re.compile(r"^(kira|pira|queira|cara|chefe)\s+")
_CREATION_VERBS = {
    "criar","cria","crie","gerar","gera","gere","fazer","faz","faca","montar","monta","monte",
    "produzir","produza","elaborar","elabora","elabore","construir","preparar","desenvolver","escrever","redigir","formatar",
}
_NEGATIVE_PATTERNS = (r"\bfaz sentido\b", r"\bfaz ela sumir\b", r"\bfaz uma pergunta\b")
_EXPLANATORY_QUESTION_PATTERNS = (
    r"\bcomo funciona\b",
    r"\bcomo eu uso\b",
    r"\bcomo usar\b",
    r"\beu teria que\b",
    r"\bteria que\b",
    r"\bpreciso baixar\b",
    r"\bbaixar o programa\b",
    r"\bso colocar api\b",
    r"\bcolocar api\b",
    r"\bmais alguma (outra )?coisa\b",
)
_EXPLICIT_CREATION_PATTERNS = (
    r"\bcria(r| um| uma)?\b",
    r"\bcrie\b",
    r"\bgera(r| um| uma)?\b",
    r"\bgere\b",
    r"\bfaz( um| uma)?\b",
    r"\bfaca( um| uma)?\b",
    r"\bmonta(r| um| uma)?\b",
    r"\bmonte\b",
    r"\bescreve(r)? (um|uma|arquivo|codigo|script|prompt)\b",
    r"\bcria um codigo\b",
    r"\bgera um pdf\b",
    r"\bcria uma apresentacao\b",
    r"\bfaz uma planilha\b",
    r"\bcria um prompt\b",
)
# Pastas, notas de texto e compactação são feitas no PC pela tool `arquivos`, não pelo Manus.
_LOCAL_FILE_OP_PATTERNS = (
    r"\b(cria|crie|criar|faz|faca|fazer|monta|monte|gera)( uma| a)?( nova)? pasta\b",
    r"\bbloco de notas\b",
    r"\bnotepad\b",
    r"\b(arquivo de texto|txt|nota)\b",
    r"\b(renomei?a|renomear|compacta|compactar|descompacta|descompactar|extrai|extrair|zipa|zipar)\b",
)
_PDF_INFO_PATTERNS = (
    r"\bo que e pdf\b",
    r"\bme explique o que e pdf\b",
    r"\bqual app abre pdf\b",
    r"\bcomo converter pdf\b",
    r"\bcade o pdf\b",
    r"\babre o pdf\b",
    r"\babre a pasta do pdf\b",
)
_ARTIFACT_HINTS = {
    "pdf": ["pdf", "arquivo pdf", "documento pdf", "relatorio em pdf", "apostila em pdf"],
    "document": ["documento", "docx", "word", "relatorio", "trabalho", "artigo", "pesquisa", "apostila"],
    "text": ["texto", "arquivo de texto", "txt", "markdown", "md", "redacao", "resumo"],
    "prompt": ["prompt", "comando para ia", "comando para chatgpt", "prompt para video", "prompt para imagem"],
    "python": ["python", "script python", "arquivo python", " py "],
    "html": ["html", "pagina html", "landing page", "frontend"],
    "javascript": ["javascript", "node", "nodejs", " js "],
    "csharp": ["csharp", "c#"],
    "slides": ["slide", "slides", "apresentacao", "powerpoint", "ppt", "pptx"],
    "spreadsheet": ["planilha", "excel", "xlsx", "tabela"],
    "image": ["imagem", "foto", "capa", "banner", "thumbnail", "arte"],
    "screenshot": ["screenshot", "print", "captura de tela"],
    "archive": ["zip", "arquivo compactado", "pacote compactado"],
    "code": ["codigo", "script", "programa", "app", "sistema", "funcao", "classe", "arquivo de codigo", "css", "json", "c++", "cpp", "java"],
}


def normalize_creation_text(text: str) -> str:
    value = str(text or "").lower().strip()
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = _WAKE_PREFIX.sub("", value)
    for old, new in _REPLACEMENTS.items():
        value = value.replace(old, new)
    return re.sub(r"\s+", " ", value).strip()


def detect_creation_task_command(text: str) -> CreationTaskCommand | None:
    raw = str(text or "").strip()
    normalized = normalize_creation_text(raw)
    if not normalized:
        return None
    for pat in _NEGATIVE_PATTERNS:
        if re.search(pat, normalized):
            return None
    if any(re.search(pat, normalized) for pat in _EXPLANATORY_QUESTION_PATTERNS):
        logger.info("CREATION_TASK_GATE_REJECTED reason=explanatory_question")
        return None
    if any(re.search(pat, normalized) for pat in _PDF_INFO_PATTERNS):
        logger.info("CREATION_TASK_PDF_INFO_NOT_CREATION")
        return None
    if any(re.search(pat, normalized) for pat in _LOCAL_FILE_OP_PATTERNS):
        logger.info("CREATION_TASK_NOT_MANUS reason=local_file_op")
        return None
    pdf_request_without_verb = bool(
        "pdf" in normalized
        and re.search(r"\b(preciso|quero|necessito|relatorio|documento)\b", normalized)
        and not re.search(r"\b(cade|abre|abrir|explique|converter|converte|app)\b", normalized)
    )
    has_creation_verb = any(re.search(pat, normalized) for pat in _EXPLICIT_CREATION_PATTERNS)
    if not has_creation_verb and not pdf_request_without_verb:
        logger.info("CREATION_TASK_GATE_REJECTED reason=no_creation_verb")
        return None
    task_type = "unknown"
    explicit_order = (
        "pdf", "slides", "prompt", "spreadsheet", "python", "html",
        "javascript", "csharp", "code", "text", "document",
    )
    hint_text = f" {normalized.replace('area de trabalho', 'desktop')} "  # destino, não "trabalho" escolar
    for t in explicit_order:
        hints = _ARTIFACT_HINTS[t]
        if any(h in hint_text for h in hints):
            task_type = t
            break
    if task_type == "unknown" and not any(token in normalized for token in ("cria", "ger", "mont", "faz", "elabor", "produ")):
        return None
    if task_type not in {"pdf", "slides", "prompt", "text", "document", "code", "python", "html", "javascript", "csharp", "spreadsheet"}:
        logger.info("CREATION_TASK_NOT_MANUS reason=local_command")
        return None
    if task_type == "document":
        task_type = "text"
    if task_type in {"text", "pdf", "spreadsheet"}:
        # documento, PDF, planilha e trabalho escolar: a tool 'documento' da própria Kira faz (services/documents.py),
        # sem o Manus (chave paga que passou a dar 401 e deixava a Kira dizendo que não dava para criar)
        logger.info("CREATION_TASK_NOT_MANUS reason=local_document_tool kind=%s", task_type)
        return None
    if task_type in {"python", "html", "javascript", "csharp"}:
        task_type = "code"
    logger.info("CREATION_TASK_KIND_DETECTED kind=%s", "slide" if task_type == "slides" else task_type)
    collect = task_type in {"pdf", "slides"}
    if task_type == "text" and re.search(r"\b(?:com a frase|com o texto)\b", normalized):
        collect = False
    destination_mode, destination_hint = detect_creation_task_destination(normalized, task_type)
    has_image = bool(re.search(r"\b(imagem|foto|figura|ilustracao|capa)\b", normalized))
    return CreationTaskCommand(
        task_type=task_type, raw_text=raw, normalized_text=normalized,
        confidence=0.9 if task_type != "unknown" else 0.65,
        requested_output=task_type, title_hint="", should_collect_more_details=collect,
        destination_mode=destination_mode, destination_hint=destination_hint, has_image=has_image,
    )


_PROVISIONAL_PATTERNS = {
    "pdf": (
        "cria um pdf", "criar um pdf", "gera um pdf", "gerar um pdf",
        "faz um pdf", "fazer um pdf", "monta um pdf", "pdf para mim",
    ),
    "text": ("cria um arquivo", "cria um texto"),
    "slides": ("cria um slide", "cria uma apresentacao"),
    "prompt": ("cria um prompt",),
    "code": ("cria um codigo",),
}


def detect_creation_task_provisional(text: str) -> str | None:
    normalized = normalize_creation_text(text)
    if not normalized:
        return None
    for kind, phrases in _PROVISIONAL_PATTERNS.items():
        if any(phrase in normalized for phrase in phrases):
            return kind
    return None


def detect_creation_task_destination(text: str, kind: str = "") -> tuple[str, str]:
    normalized = normalize_creation_text(text)
    default_terms = (
        "pasta padrao", "no padrao", "onde sempre salva", "deixa no padrao",
        "pasta de pdfs", "pasta de textos", "pasta de slides", "pasta de prompts",
    )
    if any(term in normalized for term in default_terms):
        logger.info("CREATION_TASK_DESTINATION_DETECTED mode=default kind=%s", kind or "unknown")
        return "default", str(kind or "")
    for term, hint in (
        ("area de trabalho", "desktop"), ("desktop", "desktop"),
        ("documentos", "documents"), ("downloads", "downloads"),
        ("pasta kira", "kira"), ("no projeto", "project"),
    ):
        if term in normalized and any(verb in normalized for verb in ("salv", "deixa", "coloca")):
            logger.info("CREATION_TASK_DESTINATION_DETECTED mode=specific path=%s", hint)
            return "specific", hint
    raw_path = re.search(r"\b[a-zA-Z]:[\\/][^\n]+", str(text or ""))
    if raw_path and any(verb in normalized for verb in ("salv", "deixa", "coloca")):
        path = raw_path.group(0).strip(" .")
        logger.info("CREATION_TASK_DESTINATION_DETECTED mode=specific path=%s", path)
        return "specific", path
    logger.info("CREATION_TASK_DESTINATION_MISSING mode=ask_later")
    return "ask_later", ""


_REFINEMENT_PREFIXES = (
    "com no minimo", "coloca", "tambem", "e coloca", "quero com",
    "com imagem", "com grafico", "com capa", "com fontes", "so uma pagina",
    "de uma pagina", "sobre ",
)
_SUBMIT_PHRASES = {"pode fazer", "manda", "pronto", "e isso", "isso e tudo"}


def is_creation_task_refinement(text: str) -> bool:
    normalized = normalize_creation_text(text)
    if not normalized or any(term in normalized for term in ("cade ", "onde salvou", "abre a pasta", "abre onde")):
        return False
    return any(normalized.startswith(prefix) for prefix in _REFINEMENT_PREFIXES)


def is_creation_task_submit_confirmation(text: str) -> bool:
    return normalize_creation_text(text) in _SUBMIT_PHRASES


def build_creation_task_ack(
    kind: str,
    source: str,
    *,
    waiting_for_details: bool = False,
    draft_id: str = "",
    job_id: str = "",
    command: CreationTaskCommand | None = None,
    executor_available: bool = True,
    unavailable_reason: str = "",
) -> dict[str, object]:
    task_kind = str(kind or "arquivo").upper()
    voice_source = str(source or "").strip().lower() in {"voice", "speech"}
    destination_mode = str(getattr(command, "destination_mode", "ask_later") or "ask_later")
    has_image = bool(getattr(command, "has_image", False))
    panel_text = format_manus_task_ack(
        kind=task_kind,
        destination_mode=destination_mode,
        waiting_for_details=waiting_for_details,
    ).text
    label = {
        "pdf": "esse PDF", "text": "esse texto", "slides": "essa apresentacao",
        "prompt": "esse prompt", "code": "esse codigo", "spreadsheet": "essa planilha",
    }.get(str(kind or "").lower(), f"esse {task_kind}")
    if str(kind or "").lower() == "pdf" and has_image:
        label = "esse PDF com imagem"
    speech_text = panel_text
    identity = str(draft_id or job_id or "").strip()
    dedupe_key = f"creation_task_voice_ack:{identity}" if identity else ""
    if not executor_available:
        reason = str(unavailable_reason or "executor_unavailable").strip() or "executor_unavailable"
        if reason in {"manus_disabled", "disabled", "env_disabled"}:
            panel_text = format_manus_task_ack(
                kind=kind,
                destination_mode=destination_mode,
                raw_text="O Manus esta desativado agora. Nao consegui enviar essa criacao. Posso tentar quando o Manus estiver ativo.",
            ).text
            speech_text = panel_text
            logger.info("CREATION_TASK_ACK_BLOCKED reason=manus_disabled")
        else:
            panel_text = format_manus_task_ack(
                kind=kind,
                destination_mode=destination_mode,
                raw_text="O Manus nao esta disponivel agora. A tarefa nao foi enviada.",
            ).text
            speech_text = panel_text
            logger.info("CREATION_TASK_ACK_BLOCKED reason=manus_unavailable")
        logger.info("CREATION_TASK_NO_FALSE_SUCCESS")
        return {
            "panel_text": panel_text,
            "speech_text": speech_text,
            "should_speak": voice_source,
            "dedupe_key": dedupe_key,
        }
    if voice_source:
        logger.info(
            "CREATION_TASK_ACK_CONTEXT_BUILT kind=%s has_destination=%s has_image=%s",
            str(kind or "unknown"), str(destination_mode != "ask_later").lower(), str(has_image).lower(),
        )
        logger.info("CREATION_TASK_ACK_VOICE_ALLOWED source=speech kind=%s", str(kind or "unknown"))
        logger.info("CREATION_TASK_ACK_BUILD kind=%s mode=voice_exact", str(kind or "unknown"))
        if dedupe_key:
            logger.info("CREATION_TASK_ACK_DEDUPE_KEY key=%s", dedupe_key)
    else:
        logger.info("CREATION_TASK_ACK_TEXT_ONLY source=text kind=%s", str(kind or "unknown"))
    logger.info("CREATION_TASK_ACK_GEMINI_NORMAL_BLOCKED")
    logger.info("CREATION_TASK_ACK_NO_GEMINI_NORMAL")
    logger.info("CREATION_TASK_ACK_NO_NORMAL_GEMINI")
    logger.info("CREATION_TASK_ACK_NO_GHOST_REPLY")
    return {
        "panel_text": panel_text,
        "speech_text": speech_text,
        "should_speak": voice_source,
        "dedupe_key": dedupe_key,
    }
