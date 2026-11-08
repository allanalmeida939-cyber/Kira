from __future__ import annotations

import logging
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)

_NEGATION_PREFIXES = (
    "nao",
    "nunca",
    "jamais",
)
_NEGATIVE_ACTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "open_web": (
        r"\bnao\s+(?:abre|abrir|abra|abrindo)\b",
        r"\bnao\s+e\s+(?:para|pra)\s+abrir\b",
        r"\bnao\s+quero\s+abrir\b",
        r"\bnao\s+era\s+(?:para|pra)\s+abrir\b",
        r"\beu\s+nao\s+pedi\s+(?:para|pra)\s+abrir\b",
        r"\bnao\b.*\babrir\b.*\bnao\b",
        r"\bnao\s+faz\s+isso\b",
    ),
    "close": (
        r"\bnao\s+(?:fecha|fechar|feche)\b",
    ),
    "click": (
        r"\bnao\s+(?:clica|clicar|clique)\b",
    ),
    "show_panel": (
        r"\bnao\s+(?:mostra|mostrar|mostre)\b",
        r"\bnao\s+e\s+(?:para|pra)\s+mostrar\b",
    ),
    "hide_panel": (
        r"\bnao\s+(?:oculta|ocultar|oculte|esconde|esconder|tira|tirar)\b",
    ),
}
_INVALID_WEB_TARGETS = {
    "",
    "n",
    "nao",
    "não",
    "nao abrir",
    "não abrir",
    "isso",
    "isso ai",
    "isso daí",
    "isso dai",
}
_INVALID_WEB_TARGET_PATTERNS = (
    r"^\bnao\b.*$",
    r"^\bnao\s+abrir\b.*$",
    r"^\bnao\s+e\s+(?:para|pra)\s+abrir\b.*$",
)
_KNOWN_WEB_TARGET_ALIASES = frozenset(
    {"google", "youtube", "gmail", "github", "instagram", "whatsapp", "whatsapp web", "spotify"}
)
_FILE_ARTIFACT_TARGET_TERMS = frozenset(
    {
        "paste", "pasta", "arquivo", "pdf", "de breve", "pra mim", "para mim",
        "onde ta", "onde esta", "documentos", "downloads", "area de trabalho", "desktop",
    }
)


class FuzzyCommandMatcher:
    """Matcher robusto para comandos com erros leves de STT."""

    def __init__(self, similarity_threshold: float = 0.75) -> None:
        self.similarity_threshold = max(min(float(similarity_threshold), 1.0), 0.1)
        self.commands: dict[str, list[str]] = {
            "voice_register": [
                "cadastra minha voz",
                "cadastrar minha voz",
                "salva minha voz",
                "salvar minha voz",
                "grava minha voz",
                "gravar minha voz",
                "registra minha voz",
                "registrar minha voz",
                "guarda minha voz",
                "guardar minha voz",
            ],
            "voice_list": [
                "quem voce conhece",
                "quem vc conhece",
                "quem ce conhece",
                "lista perfis",
                "listar perfis",
                "lista os perfis",
                "quais perfis",
                "que perfis",
            ],
            "voice_delete": [
                "esquece minha voz",
                "esquecer minha voz",
                "deleta meu perfil",
                "deletar meu perfil",
                "apaga minha voz",
                "apagar minha voz",
                "remove meu perfil",
                "remover meu perfil",
            ],
            "voice_reset": [
                "volta a falar",
                "reseta a voz",
                "resetar a voz",
                "reseta voz",
                "voz de volta",
                "fala de novo",
                "tenta falar",
            ],
            "open_web": [
                "abre o",
                "abrir o",
                "abre a",
                "abrir a",
                "abre",
                "abrir",
                "navega para",
                "navegar para",
                "vai para",
                "acessa",
                "acessar",
            ],
            "search_web": [
                "pesquisa sobre",
                "pesquisar sobre",
                "procura sobre",
                "procurar sobre",
                "busca sobre",
                "buscar sobre",
                "pesquisa",
                "pesquisar",
                "procura",
                "procurar",
                "busca",
                "buscar",
            ],
        }

    @staticmethod
    def normalize(text: str) -> str:
        value = str(text or "").lower().strip()
        if not value:
            return ""
        value = unicodedata.normalize("NFD", value)
        value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
        value = re.sub(r"[^\w\s]", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value

    @classmethod
    def is_negative_command(cls, text: str, action: str) -> bool:
        normalized = cls.normalize(text)
        if not normalized:
            return False
        for pattern in _NEGATIVE_ACTION_PATTERNS.get(str(action or "").strip(), ()):
            if re.search(pattern, normalized):
                return True
        if action in {"open_web", "close", "click", "show_panel", "hide_panel"}:
            return normalized.startswith(_NEGATION_PREFIXES) and any(
                token in normalized
                for token in ("abr", "fech", "clic", "mostr", "ocult", "escond", "tira")
            )
        return False

    @classmethod
    def is_valid_imperative_command(cls, text: str, action: str) -> bool:
        normalized = cls.normalize(text)
        if not normalized:
            return False
        if cls.is_negative_command(normalized, action):
            return False
        if action == "open_web":
            imperative_starts = (
                "abre",
                "abrir",
                "abra",
                "acessa",
                "acessar",
                "navega para",
                "navegar para",
            )
            return normalized.startswith(imperative_starts)
        if action == "search_web":
            return normalized.startswith(("pesquisa", "pesquisar", "procura", "procurar", "busca", "buscar"))
        return True

    @classmethod
    def is_invalid_open_web_target(cls, target: str) -> bool:
        normalized = cls.normalize(target)
        if normalized in _INVALID_WEB_TARGETS:
            return True
        for pattern in _INVALID_WEB_TARGET_PATTERNS:
            if re.search(pattern, normalized):
                return True
        if any(term in normalized for term in _FILE_ARTIFACT_TARGET_TERMS):
            logger.info("OPEN_WEB_TARGET_REJECTED reason=local_file_phrase target=%r", normalized)
            logger.info("FUZZY_ROUTE_BLOCKED reason=local_file_command")
            return True
        raw_target = str(target or "").strip()
        if normalized in _KNOWN_WEB_TARGET_ALIASES:
            return False
        # STT costuma perder uma letra de nomes conhecidos (por exemplo,
        # "yotube"). Isso ainda é um alvo de site, não uma frase natural
        # ambígua; a validação do comando continua bloqueando negações e
        # pedidos de arquivos logo acima.
        if normalized and any(cls.similarity(normalized, alias) >= 0.72 for alias in _KNOWN_WEB_TARGET_ALIASES):
            return False
        if re.match(r"^https?://[^\s]+\.[^\s]+$", raw_target, flags=re.IGNORECASE):
            return False
        if re.match(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:[/#:?].*)?$", raw_target, flags=re.IGNORECASE):
            return False
        if " " in normalized:
            logger.info("OPEN_WEB_TARGET_REJECTED reason=ambiguous_natural_phrase target=%r", normalized)
            return True
        logger.info("OPEN_WEB_TARGET_REJECTED reason=no_domain_or_known_alias target=%r", normalized)
        return True

    @staticmethod
    def similarity(a: str, b: str) -> float:
        return SequenceMatcher(None, str(a or ""), str(b or "")).ratio()

    @staticmethod
    def _sorted_tokens(text: str) -> str:
        return " ".join(sorted(str(text or "").split()))

    def _iter_windows(self, text_norm: str, phrase_norm: str) -> list[str]:
        text_tokens = text_norm.split()
        phrase_tokens = phrase_norm.split()
        if not text_tokens or not phrase_tokens:
            return []
        min_size = max(1, len(phrase_tokens) - 1)
        max_size = min(len(text_tokens), len(phrase_tokens) + 2)
        windows: list[str] = []
        for window_size in range(min_size, max_size + 1):
            for start in range(0, len(text_tokens) - window_size + 1):
                windows.append(" ".join(text_tokens[start : start + window_size]))
        return windows

    def _phrase_score(self, text_norm: str, phrase_norm: str) -> float:
        if not text_norm or not phrase_norm:
            return 0.0
        if text_norm == phrase_norm:
            return 1.0

        best_score = 0.0
        if phrase_norm in text_norm:
            length_ratio = min(len(phrase_norm) / max(len(text_norm), 1), 1.0)
            best_score = max(best_score, 0.95 + (length_ratio * 0.05))

        phrase_sorted = self._sorted_tokens(phrase_norm)
        best_score = max(
            best_score,
            self.similarity(text_norm, phrase_norm),
            self.similarity(self._sorted_tokens(text_norm), phrase_sorted),
        )

        # Em frase bem mais longa que o comando, só a frase exata (ou as mesmas palavras) conta: a semelhança de
        # letras de um pedaço ("saiu de novo" ~ "fala de novo") disparava comandos no meio de perguntas.
        long_text = len(text_norm.split()) > len(phrase_norm.split()) + 2
        for window in self._iter_windows(text_norm, phrase_norm):
            if window == phrase_norm:
                return 1.0
            window_sorted = self._sorted_tokens(window)
            if window_sorted == phrase_sorted:
                best_score = max(best_score, 0.96)
            if long_text:
                continue
            best_score = max(
                best_score,
                self.similarity(window, phrase_norm),
                self.similarity(window_sorted, phrase_sorted),
            )
        return best_score

    def match(self, text: str) -> Optional[tuple[str, float, str]]:
        text_norm = self.normalize(text)
        if not text_norm:
            return None

        best_action: str | None = None
        best_score = 0.0
        best_phrase: str | None = None

        for action, phrases in self.commands.items():
            for phrase in phrases:
                phrase_norm = self.normalize(phrase)
                score = self._phrase_score(text_norm, phrase_norm)
                if score > best_score:
                    best_action = action
                    best_score = score
                    best_phrase = phrase

        if best_action is None or best_phrase is None or best_score < self.similarity_threshold:
            return None
        return best_action, round(best_score, 4), best_phrase

    def extract_target(self, text: str, action: str) -> str:
        if action == "open_web":
            explicit_target = re.search(
                r"(https?://[^\s]+|(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:[/#:?][^\s]*)?)",
                str(text or ""),
                flags=re.IGNORECASE,
            )
            if explicit_target:
                return explicit_target.group(1)
        text_norm = self.normalize(text)
        if not text_norm:
            return ""
        phrases = self.commands.get(str(action or "").strip(), [])
        best_target = ""
        best_score = 0.0

        for phrase in phrases:
            phrase_norm = self.normalize(phrase)
            if not phrase_norm:
                continue
            score = self._phrase_score(text_norm, phrase_norm)
            if score < self.similarity_threshold:
                continue

            target = ""
            if text_norm.startswith(f"{phrase_norm} "):
                target = text_norm[len(phrase_norm) :].strip()
            elif text_norm == phrase_norm:
                target = ""
            elif phrase_norm in text_norm:
                _prefix, _sep, suffix = text_norm.partition(phrase_norm)
                target = suffix.strip()
            else:
                text_tokens = text_norm.split()
                phrase_tokens = phrase_norm.split()
                if len(text_tokens) > len(phrase_tokens):
                    target = " ".join(text_tokens[len(phrase_tokens) :]).strip()

            if score > best_score:
                best_score = score
                best_target = target

        target = best_target.strip()
        # A extração é um contrato de dados: preserve o texto capturado mesmo
        # quando a camada de execução ainda vai rejeitá-lo. Antes, a validação
        # retornava "" para qualquer alvo sem domínio e apagava justamente os
        # alvos fuzzy, como "abre o yotube".
        return target


def handle_open_web(text: str) -> dict:
    matcher = FuzzyCommandMatcher()
    normalized = matcher.normalize(text)
    if not matcher.is_valid_imperative_command(normalized, "open_web"):
        return {"handled": False}
    target = matcher.extract_target(text, "open_web")
    if target and not matcher.is_invalid_open_web_target(target):
        return {"handled": False, "action": "open_web", "target": target}
    raw_target = re.sub(r"^(abre|abrir|abra|acessa|acessar|navega para|navegar para)\s+", "", normalized).strip()
    if raw_target in _KNOWN_WEB_TARGET_ALIASES:
        return {"handled": False}
    logger.info("OPEN_WEB_TARGET_REJECTED reason=ambiguous_natural_phrase target=%s", raw_target)
    logger.info("LOCAL_COMMAND_TEXT_ONLY source=open_web_rejected")
    logger.info("LOCAL_COMMAND_GEMINI_DISABLED reason=open_web_rejected")
    logger.info("LOCAL_COMMAND_VOICE_DISABLED reason=open_web_rejected")
    logger.info("LOCAL_COMMAND_NO_SPEECH_STATE_CHANGE")
    logger.info("LOCAL_COMMAND_TURN_DONE source=open_web_rejected")
    return {
        "handled": True,
        "blocked": True,
        "response": "Interface: nao identifiquei um site valido para abrir.",
        "target": raw_target,
    }


__all__ = ["FuzzyCommandMatcher", "handle_open_web"]
