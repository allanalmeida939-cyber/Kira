from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


@dataclass
class VolumeIntent:
    intent: str
    value: int | None = None
    direction: str | None = None
    step: str | None = None
    delta: int | None = None


def normalize_text(text: str | None) -> str:
    raw = (text or "").strip().lower()
    no_accents = "".join(ch for ch in unicodedata.normalize("NFD", raw) if unicodedata.category(ch) != "Mn")
    normalized = re.sub(r"\s+", " ", no_accents).strip()
    before_fixes = normalized
    fixes = (
        (r"\balmenta\b", "aumenta"),
        (r"\balmentar\b", "aumentar"),
        (r"\baomenta\b", "aumenta"),
        (r"\bporcento\b", "por cento"),
    )
    for pattern, replacement in fixes:
        normalized = re.sub(pattern, replacement, normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if normalized != before_fixes:
        logger.info('LOCAL_COMMAND_STT_AUTOCORRECTED raw="%s" normalized="%s"', str(text or "")[:180], normalized[:180])
        logger.info("LOCAL_COMMAND_STT_FUZZY_MATCHED")
    return normalized


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))


def _extract_explicit_number(text: str) -> int | None:
    match = re.search(r"(-?\d+)\s*(?:%|por cento)?", text)
    if match:
        return _clamp(int(match.group(1)))
    return None


def _extract_word_number(text: str, *, require_percentage: bool = True) -> int | None:
    units = {
        "zero": 0,
        "um": 1,
        "uma": 1,
        "dois": 2,
        "duas": 2,
        "tres": 3,
        "quatro": 4,
        "cinco": 5,
        "seis": 6,
        "sete": 7,
        "oito": 8,
        "nove": 9,
    }
    teens = {
        "dez": 10,
        "onze": 11,
        "doze": 12,
        "treze": 13,
        "catorze": 14,
        "quatorze": 14,
        "quinze": 15,
        "dezesseis": 16,
        "dezessete": 17,
        "dezoito": 18,
        "dezenove": 19,
    }
    tens = {
        "vinte": 20,
        "trinta": 30,
        "quarenta": 40,
        "cinquenta": 50,
        "sessenta": 60,
        "setenta": 70,
        "oitenta": 80,
        "noventa": 90,
        "cem": 100,
        "cento": 100,
    }

    if require_percentage and "por cento" not in text and "%" not in text:
        return None

    tokens = re.findall(r"[a-z]+", text)
    for i, token in enumerate(tokens):
        if token in teens:
            return teens[token]
        if token in units:
            return units[token]
        if token in tens:
            base = tens[token]
            if base == 100:
                return 100
            if i + 2 < len(tokens) and tokens[i + 1] == "e" and tokens[i + 2] in units:
                return _clamp(base + units[tokens[i + 2]])
            return base
    return None


def extract_standalone_volume_percent(command_text: str) -> int | None:
    text = normalize_text(command_text).strip(" .!?")
    patterns = (
        r"^-?\d+\s*%$",
        r"^-?\d+\s+por cento$",
        r"^em\s+-?\d+\s*%?$",
        r"^(?:zero|dez|vinte|trinta|quarenta|cinquenta|sessenta|setenta|oitenta|noventa|cem)\s+por cento$",
        r"^em\s+(?:zero|dez|vinte|trinta|quarenta|cinquenta|sessenta|setenta|oitenta|noventa|cem)$",
    )
    if not any(re.match(pattern, text) for pattern in patterns):
        return None
    number = _extract_explicit_number(text)
    return number if number is not None else _extract_word_number(text, require_percentage=False)


_PLAYER_RE = re.compile(r"\b(musicas?|videos?|youtube|player|clipe)\b")
# "baixa o livro no gutenberg" virava "abaixa o volume" (teste de 2026-10-30): baixar + coisa/site = download
_DOWNLOAD_RE = re.compile(
    r"\b(baixa|baixar|baixe|baixando)\b.*\b(videos?|clipes?|filmes?|episodios?|mp3|mp4|link|arquivos?|fotos?|livros?|pdfs?|epub|"
    r"documentos?|apps?|aplicativos?|programas?|jogos?|imagens?|planilhas?|instalador|driver|torrent|site|pagina|internet)\b"
    r"|\b(entra|abre|acessa|vai)\b.*\b(baixa|baixar|baixe)\b|\b(baixa|baixar|baixe)\b.*\.(com|org|br|net|io)\b"
)


_HOME_DEVICE_RE = re.compile(r"\b(tv|tvs|televis\w*|chromecast|caixa de som|alexa|google home|smart ?tv)\b")


def is_volume_command_candidate(command_text: str, *, has_recent_context: bool = False) -> bool:
    text = normalize_text(command_text).strip(" .!?")
    # "baixa esse video em 480" e download, nao volume (virava volume 100%)
    if _DOWNLOAD_RE.search(text) and not re.search(r"\b(volume|som)\b", text):
        return False
    # volume "da musica/do video" e do player da Kira (tool control_music), nao do Windows
    if _PLAYER_RE.search(text):
        return False
    # volume/mudo "da TV", do Chromecast ou da caixa de som é da casa inteligente (tool casa), não do Windows
    # (2026-10-31: "deixa a TV no mudo" mutava o PC)
    if _HOME_DEVICE_RE.search(text):
        return False
    if extract_standalone_volume_percent(text) is not None:
        return True
    if any(token in text for token in ("volume", "som", "muta", "silencia", "desmuta", "mudo")):
        return True
    if re.search(r"\b(aumenta|aumentar|sobe|subir|diminui|diminuir|abaixa|baixar|baixa|reduz|reduzir)\b", text):
        return True
    if has_recent_context and any(token in text for token in ("diminui mais", "abaixa mais", "aumenta mais", "sobe mais")):
        return True
    return bool(
        re.match(
            r"^(?:coloca|coloque|deixa|bota|poe|põe)\s+(?:o\s+volume\s+)?(?:em|no)?\s*"
            r"(?:-?\d+\s*%?|zero|dez|vinte|trinta|quarenta|cinquenta|sessenta|setenta|oitenta|noventa|cem)(?:\s+por cento)?$",
            text,
        )
    )


def parse_volume_intent(command_text: str) -> VolumeIntent:
    text = normalize_text(command_text)

    if any(token in text for token in ("tira do mudo", "ativar som", "ativa som", "ativa o som", "voltar som", "volta o som", "coloca som de volta", "coloca o som de volta", "desmuta", "desmutar", "dessilenciar", "reativar o som", "som novamente")):
        return VolumeIntent(intent="unmute_volume")
    if any(token in text for token in ("silenciar", "silencia", "muta", "mutar", "tirar som", "tira o som", "sem som", "no mudo", "deixa no mudo", "mute", "mudo")):
        return VolumeIntent(intent="mute_volume")

    explicit_number = _extract_explicit_number(text)
    if explicit_number is None:
        explicit_number = _extract_word_number(
            text,
            require_percentage=not bool(
                re.search(r"\b(volume|coloca|coloque|deixa|bota|poe)\b", text)
            ),
        )

    if explicit_number is not None:
        return VolumeIntent(intent="set_volume_absolute", value=explicit_number)

    up_tokens = ("aumenta", "aumentar", "sobe", "subir", "mais alto", "mais alta", "aumente")
    down_tokens = ("diminui", "diminuir", "abaixa", "baixar", "baixa", "mais baixo", "mais baixa", "reduz", "reduzir")
    if any(token in text for token in up_tokens):
        return VolumeIntent(intent="adjust_volume_relative", direction="up", step="default", delta=10)
    if any(token in text for token in down_tokens):
        return VolumeIntent(intent="adjust_volume_relative", direction="down", step="default", delta=-10)

    return VolumeIntent(intent="unknown")
