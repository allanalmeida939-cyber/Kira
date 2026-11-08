from __future__ import annotations

import time
from dataclasses import dataclass
from difflib import SequenceMatcher


CONTEXT_PRIORITY = {
    "erro": 7,
    "confirmacao": 6,
    "login": 5,
    "formulario": 4,
    "carregamento": 3,
    "sucesso": 2,
    "neutro": 1,
}

_CTX_KEYWORDS = {
    "erro": ("erro", "falha", "nao foi possivel", "error", "failed"),
    "confirmacao": ("confirmar", "deseja continuar", "tem certeza", "aceitar", "cancelar"),
    "login": ("login", "senha", "entrar", "autenticacao", "sign in"),
    "formulario": ("formulario", "campo", "preencher", "input", "enviar"),
    "carregamento": ("carregando", "aguarde", "loading", "processando"),
    "sucesso": ("sucesso", "concluido", "concluida", "enviado", "salvo", "success"),
}


def _normalize_text(value: str) -> str:
    text = str(value or "").lower()
    table = str.maketrans(
        {
            "á": "a",
            "à": "a",
            "â": "a",
            "ã": "a",
            "é": "e",
            "ê": "e",
            "í": "i",
            "ó": "o",
            "ô": "o",
            "õ": "o",
            "ú": "u",
            "ç": "c",
        }
    )
    return " ".join(text.translate(table).split())


@dataclass(slots=True)
class ScreenContextAnalysis:
    context: str
    subtype: str
    confidence: float
    source_keywords: list[str]
    text_excerpt: str
    timestamp: float

    def as_dict(self) -> dict[str, object]:
        return {
            "screen_context": self.context,
            "subtype": self.subtype,
            "keywords": list(self.source_keywords),
            "confidence": round(self.confidence, 2),
            "text_excerpt": self.text_excerpt,
            "timestamp": self.timestamp,
        }


class ScreenContextAnalyzer:
    def __init__(self) -> None:
        self._last_context = ""
        self._last_subtype = ""
        self._last_excerpt = ""
        self._last_emitted_at = 0.0

    def analyze_screen_context(self, vision_event: dict[str, object]) -> ScreenContextAnalysis:
        text = _normalize_text(str(vision_event.get("text_excerpt") or ""))
        keywords = [str(k) for k in vision_event.get("keywords", []) if str(k).strip()]
        meta = vision_event.get("meta", {}) if isinstance(vision_event.get("meta"), dict) else {}
        fields_count = int(meta.get("fields_count", 0) or 0)
        buttons_count = int(meta.get("buttons_count", 0) or 0)

        context = self.classify_screen_context(text, keywords, fields_count=fields_count, buttons_count=buttons_count)
        subtype = self.detect_screen_subtype(context=context, text=text, keywords=keywords)
        confidence = self._estimate_confidence(context=context, subtype=subtype, keywords=keywords, text=text)
        return self.build_screen_context_result(
            context=context,
            subtype=subtype,
            confidence=confidence,
            source_keywords=keywords,
            text_excerpt=text[:180],
            timestamp=float(vision_event.get("timestamp") or time.monotonic()),
        )

    def classify_screen_context(
        self,
        text: str,
        keywords: list[str],
        *,
        fields_count: int = 0,
        buttons_count: int = 0,
    ) -> str:
        normalized = _normalize_text(text)
        candidates: list[str] = []

        for context, tokens in _CTX_KEYWORDS.items():
            if any(token in normalized for token in tokens):
                candidates.append(context)

        # Sinal estrutural simples de formulario.
        if fields_count >= 2 and buttons_count >= 1:
            candidates.append("formulario")

        # Reuso rapido de keywords da fase 1.
        if "erro" in keywords or "falha" in keywords:
            candidates.append("erro")
        if "login" in keywords or "senha" in keywords:
            candidates.append("login")
        if "confirmar" in keywords:
            candidates.append("confirmacao")
        if "carregando" in keywords:
            candidates.append("carregamento")
        if "sucesso" in keywords:
            candidates.append("sucesso")

        if not candidates:
            return "neutro"

        return sorted(set(candidates), key=lambda item: CONTEXT_PRIORITY.get(item, 0), reverse=True)[0]

    def detect_screen_subtype(self, *, context: str, text: str, keywords: list[str]) -> str:
        normalized = _normalize_text(text)
        if context == "erro":
            if "login" in normalized or "senha" in normalized:
                return "falha_login"
            if "carregar" in normalized:
                return "erro_carregamento"
            return "erro_generico"
        if context == "login":
            if "senha" in normalized:
                return "senha"
            return "tela_login"
        if context == "carregamento":
            return "loading"
        if context == "sucesso":
            if "salvo" in normalized:
                return "salvo"
            if "enviado" in normalized:
                return "enviado"
            return "sucesso_generico"
        if context == "confirmacao":
            if "tem certeza" in normalized:
                return "tem_certeza"
            if "deseja continuar" in normalized:
                return "deseja_continuar"
            return "confirmar"
        if context == "formulario":
            return "preenchimento"
        return "desconhecido"

    def build_screen_context_result(
        self,
        *,
        context: str,
        subtype: str,
        confidence: float,
        source_keywords: list[str],
        text_excerpt: str,
        timestamp: float,
    ) -> ScreenContextAnalysis:
        return ScreenContextAnalysis(
            context=context,
            subtype=subtype,
            confidence=confidence,
            source_keywords=source_keywords,
            text_excerpt=text_excerpt,
            timestamp=timestamp,
        )

    def is_duplicate_context(self, result: ScreenContextAnalysis, *, now: float | None = None) -> bool:
        current_now = now or time.monotonic()
        if not self._last_context:
            self._remember(result, current_now=current_now)
            return False

        if result.context == self._last_context and result.subtype == self._last_subtype:
            sim = SequenceMatcher(None, result.text_excerpt, self._last_excerpt).ratio() if result.text_excerpt and self._last_excerpt else 0.0
            if sim >= 0.95 and (current_now - self._last_emitted_at) < 8.0:
                return True

        self._remember(result, current_now=current_now)
        return False

    def _remember(self, result: ScreenContextAnalysis, *, current_now: float) -> None:
        self._last_context = result.context
        self._last_subtype = result.subtype
        self._last_excerpt = result.text_excerpt
        self._last_emitted_at = current_now

    def _estimate_confidence(self, *, context: str, subtype: str, keywords: list[str], text: str) -> float:
        base = 0.46
        if context != "neutro":
            base += 0.18
        base += min(0.18, 0.04 * len(set(keywords)))
        if subtype not in {"desconhecido", "erro_generico", "sucesso_generico"}:
            base += 0.08
        if len(text) > 40:
            base += 0.05
        return min(base, 0.94)

