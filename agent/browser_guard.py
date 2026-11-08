from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse


logger = logging.getLogger(__name__)
browser_guard_logger = logging.getLogger("browser_guard")
browser_guard_logger.setLevel(logging.INFO)
browser_guard_logger.propagate = False

_LOG_PATH = Path(__file__).resolve().parent.parent / "browser_guard.log"
if not any(
    isinstance(handler, logging.FileHandler)
    and Path(getattr(handler, "baseFilename", "")).resolve() == _LOG_PATH
    for handler in browser_guard_logger.handlers
):
    _guard_handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    _guard_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    browser_guard_logger.addHandler(_guard_handler)


_EXPLICIT_BROWSER_TRIGGERS = (
    "abra o navegador",
    "abre o navegador",
    "abrir navegador",
    "navegue para",
    "navega para",
    "va para o site",
    "vai para o site",
    "acesse o site",
    "acessa o site",
    "entre no site",
    "abra o site",
    "pesquise no navegador",
    "pesquisa no navegador",
    "busque no navegador",
    "busca no navegador",
    "me mostre no navegador",
)
_GENERIC_BROWSER_PREFIXES = (
    "abra ",
    "abre ",
    "abrir ",
    "acesse ",
    "acessa ",
    "acessar ",
    "navegue para ",
    "navega para ",
    "vai para ",
    "va para ",
    "entre no site ",
    "abra o site ",
    "acesse o site ",
)
_KNOWN_BROWSER_TARGETS = frozenset(
    {
        "youtube",
        "google",
        "gmail",
        "facebook",
        "instagram",
        "twitter",
        "x",
        "netflix",
        "spotify",
        "github",
        "whatsapp",
    }
)
_BROWSER_HOME_REQUESTS = frozenset(
    {
        "abra o navegador",
        "abre o navegador",
        "abrir navegador",
        "abrir o navegador",
        "abra navegador",
        "abre navegador",
    }
)
_FALSE_POSITIVE_PATTERNS = (
    "algum projeto",
    "alguns projetos",
    "projeto open source",
    "melhorar o",
    "como melhorar",
    "existe algum",
    "conhece algum",
    "sabe de algum",
    "qual e o melhor",
    "qual é o melhor",
    "me fale sobre",
    "o que e",
    "o que é",
    "como funciona",
)
_CONVERSATIONAL_SITE_PATTERNS = (
    "que site",
    "qual site",
    "esse site",
    "site que",
    "site bom",
    "site para",
)
_SUSPICIOUS_URL_TOKENS = (
    "%20",
    "algum",
    "projeto",
    "melhorar",
    "assistente",
    "%20%20",
)
_URL_PATTERN = re.compile(r"(https?://[^\s]+|www\.[^\s]+)", re.IGNORECASE)
_DOMAIN_PATTERN = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}(?:/[^\s]*)?)\b",
    re.IGNORECASE,
)


def _normalize_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value or ""))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.lower().strip()
    folded = re.sub(r"\s+", " ", folded)
    return folded


def _target_after_prefix(text: str) -> str:
    normalized = _normalize_text(text)
    for prefix in _GENERIC_BROWSER_PREFIXES:
        if normalized.startswith(prefix):
            return normalized[len(prefix) :].strip()
    return ""


def is_browser_home_request(text: str) -> bool:
    normalized = _normalize_text(text)
    return normalized in _BROWSER_HOME_REQUESTS


def normalize_navigable_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("www."):
        return f"https://{raw}"
    if _DOMAIN_PATTERN.fullmatch(raw):
        return f"https://{raw}"
    return raw


def extract_clean_url(text: str) -> str | None:
    raw_text = str(text or "").strip()
    if not raw_text:
        return None

    matches = _URL_PATTERN.findall(raw_text)
    if not matches:
        matches = _DOMAIN_PATTERN.findall(raw_text)
    if not matches:
        return None

    candidate = normalize_navigable_url(matches[0])
    normalized_candidate = _normalize_text(candidate)
    if not candidate or " " in candidate:
        return None
    if any(token in normalized_candidate for token in _SUSPICIOUS_URL_TOKENS):
        return None
    if not is_valid_navigable_url(candidate):
        return None
    return candidate


def is_valid_navigable_url(url: str) -> bool:
    candidate = normalize_navigable_url(url)
    if not candidate:
        return False
    if any(ch.isspace() for ch in candidate):
        return False
    normalized_candidate = _normalize_text(candidate)
    if any(token in normalized_candidate for token in _SUSPICIOUS_URL_TOKENS):
        return False
    try:
        parsed = urlparse(candidate)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc or len(parsed.netloc) < 4:
        return False
    if "." not in parsed.netloc:
        return False
    return True


def looks_like_malformed_browser_url(text: str) -> bool:
    raw_text = str(text or "").strip()
    if not raw_text:
        return False
    normalized = _normalize_text(raw_text)
    has_url_markers = bool(_URL_PATTERN.search(raw_text) or _DOMAIN_PATTERN.search(raw_text))
    if not has_url_markers:
        return False
    if extract_clean_url(raw_text) is None and any(token in normalized for token in _SUSPICIOUS_URL_TOKENS):
        return True
    return False


def requires_explicit_browser_intent(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False

    if any(pattern in normalized for pattern in _FALSE_POSITIVE_PATTERNS):
        return False
    if any(pattern in normalized for pattern in _CONVERSATIONAL_SITE_PATTERNS):
        return False

    if normalized.startswith(("http://", "https://", "www.")):
        return True
    if _DOMAIN_PATTERN.fullmatch(normalized):
        return True
    if normalized in _BROWSER_HOME_REQUESTS:
        return True
    if any(normalized.startswith(trigger) for trigger in _EXPLICIT_BROWSER_TRIGGERS):
        return True

    target = _target_after_prefix(normalized)
    if not target:
        return False
    if target in _KNOWN_BROWSER_TARGETS:
        return True
    if extract_clean_url(target) is not None:
        return True
    return False


def evaluate_browser_action(text: str) -> tuple[bool, str]:
    normalized = _normalize_text(text)
    if not normalized:
        return False, "empty"
    if not requires_explicit_browser_intent(text):
        return False, "missing_explicit_browser_intent"
    if looks_like_malformed_browser_url(text):
        return False, "malformed_url_like_input"
    candidate_url = extract_clean_url(text)
    if candidate_url and not is_valid_navigable_url(candidate_url):
        return False, "invalid_url"
    return True, "allowed"


def should_block_browser_action(user_text: str) -> bool:
    allowed, reason = evaluate_browser_action(user_text)
    if allowed:
        return False
    preview = str(user_text or "").strip()[:160]
    message = f"[BROWSER_GUARD] blocked reason={reason} text={preview!r}"
    logger.warning(message)
    browser_guard_logger.warning(message)
    return True


__all__ = [
    "browser_guard_logger",
    "evaluate_browser_action",
    "extract_clean_url",
    "is_browser_home_request",
    "is_valid_navigable_url",
    "looks_like_malformed_browser_url",
    "normalize_navigable_url",
    "requires_explicit_browser_intent",
    "should_block_browser_action",
]
