import logging
import re
import unicodedata
from enum import Enum

from .fuzzy_command_matcher import FuzzyCommandMatcher
try:
    from core.creation_task_commands import detect_creation_task_command
    _creation_task_import_error = None
except Exception as exc_core:
    try:
        from agent.core.creation_task_commands import detect_creation_task_command
        _creation_task_import_error = None
    except Exception as exc_agent:
        _creation_task_import_error = exc_agent

        def detect_creation_task_command(_text):
            return None

logger = logging.getLogger(__name__)
if _creation_task_import_error is None:
    logger.info("VISUAL_ROUTER_CREATION_TASK_IMPORT_OK")
else:
    logger.warning("VISUAL_ROUTER_CREATION_TASK_IMPORT_DEGRADED error=%s", _creation_task_import_error)


class VisualIntent(Enum):
    NONE = "none"
    QUESTION = "question"
    ACTION = "action"


LOCATE_RE = re.compile(r"\b(?:onde (?:e que )?(?:esta|ta|fica)|cade)\b")
# "Cria uma imagem de um cachorro na lua" é pedido de criação (criar_imagem), não pergunta sobre a tela.
CREATE_IMAGE_RE = re.compile(
    r"\b(?:cria|crie|criar|gera|gere|gerar|faz|faca|fazer|desenha|desenhe|desenhar|monta|monte|montar)\b"
    r".{0,40}\b(?:imagem|imagens|foto|fotos|desenho|ilustracao|arte|logo|wallpaper)\b"
)


class VisualRouter:
    VISION_DISABLED_MESSAGE = "Chefe, estou com a visão desligada. Ativa o modo visão no botão."
    VISION_CONTROL_PATTERNS = (
        r"\b(ativa|liga|ativar|ligar)\s+((a|o)\s+)?(visao|compartilhamento|tela)\b",
        r"\b(desativa|desliga|desativar|desligar)\s+((a|o)\s+)?(visao|compartilhamento|tela)\b",
    )
    VISION_ACTIVATION_REFERENCE_PATTERNS = (
        r"\b(ativei|liguei|ative|liga|ativa|ativado|ativada|ligado|ligada)\b",
        r"\bcom\s+a\s+visao\s+ligada\b",
        r"\bcom\s+o\s+monitor\s+ligado\b",
    )
    VISION_STATUS_PATTERNS = (
        r"\b((a|o)\s+)?(visao|compartilhamento)\s+(esta|ta)\s+(ativad[ao]|ligad[ao]|desativad[ao]|desligad[ao])\b",
        r"\b(voce|vc)\s+(esta|ta)\s+me\s+vendo\b",
        r"\bconsegue\s+me\s+ver\b",
        r"\bconsegue\s+ver\s+minha\s+tela\b",
    )
    VISION_QUERY_PATTERNS = (
        r"\b(voce|vc)\s+(esta|ta)\s+(vendo|vendo\s+ai|vendo\s+aqui)\s+((a\s+)?minha\s+)?tela\b",
        r"\b(voce|vc)\s+(esta|ta)\s+vendo\b",
        r"\b(voce|vc)\s+(consegue\s+)?(ver|enxergar)\s+((a\s+)?minha\s+)?tela\b",
        r"\b(voce|vc)\s+(consegue\s+)?(ver|enxergar)\s+a\s+tela\b",
        r"\b(consegue|da\s+pra)\s+(ver|enxergar)\s+((a\s+)?minha\s+)?tela\b",
        r"\b(consegue|da\s+pra)\s+(ver|enxergar)\s+a\s+tela\b",
        r"\b(ta|esta)\s+vendo\s+((a\s+)?minha\s+)?tela\b",
        r"\b(olha|analisa|descreve|descreva|le|ve)\s+((a\s+)?minha\s+|essa\s+|a\s+)?tela\b",
        r"\bolha\s+(ai|isso|aqui)\b",
        r"\bme\s+diz\s+o\s+que\s+aparece\b",
        r"\bo\s+que\s+(aparece|tem|esta|ta)\s+(na|no)\s+(tela|monitor)\b",
        r"\bo\s+que\s+tem\s+(ai|aqui)\b",
        r"\bo\s+que\s+tem\s+aberto\s+(ai|aqui)?\b",
        r"\bo\s+que\s+aparece\s+no\s+monitor\b",
        r"\bagora\s+o\s+que\s+(aparece|tem|voce\s+(ve|esta\s+vendo|ta\s+vendo))\b",
        r"\be\s+agora\b",
    )

    def __init__(self, vision_mode_enabled: bool = False) -> None:
        self._vision_enabled = bool(vision_mode_enabled)
        self.VISUAL_ACTION_KEYWORDS = ["clica", "clica em", "clicar", "clique em", "da um clique em", "de um clique em", "aperta", "aperte", "pressiona", "pressione", "tecla", "ctrl+", "control+", "ctrl c", "control c", "alt+f4", "enter", "esc", "escape", "tab", "backspace", "fecha", "fecha isso", "fecha essa janela", "fechar", "abre", "abre isso", "abrir", "digita", "digitar", "digite", "escreve", "escrever", "seleciona", "selecionar", "arrasta", "arrastar", "rola", "rolar", "scroll", "copia", "cola", "recorta", "desfaz", "seleciona tudo", "maximiza", "minimiza"]
        self.VISUAL_QUESTION_KEYWORDS = ["o que tem na tela", "que tem na tela", "o que ta na tela", "o que esta na tela", "o que voce ta vendo", "o que voce esta vendo", "o que voce esta vendo ai", "o que vc ta vendo", "o que vc esta vendo", "o que vc esta vendo ai", "o que voce ve", "o que vc ve", "o que ta vendo", "o que esta vendo", "ta vendo a tela", "ta vendo isso", "ta vendo ai", "voce ta vendo a tela", "voce ta vendo ai", "voce esta vendo a tela", "voce esta vendo ai", "vendo a tela", "vendo isso", "o que aparece ai", "o que aparece na tela", "o que aparece por ai", "o que aparece no monitor", "o que tem aberto ai", "olha ai", "olha minha tela", "me diz o que aparece", "descreve essa tela", "qual erro", "que erro", "resume pagina", "resume pagina pra mim", "resume a pagina", "olha a tela", "le a tela", "ve a tela", "ve isso", "enxerga isso", "o que esta aberto", "o que ta aberto", "me descreve a tela", "descreva a tela", "descreve o que voce ve", "descreve o que vc ve", "descreve o que ta vendo", "descreva o que voce ve", "descreva o que vc ve", "me mostra a tela", "mostra a tela", "qual e a tela", "como esta a tela"]
        self.VISUAL_KEYWORDS = ["vendo", "ve", "vejo", "ver", "veja", "olha", "olhar", "olhando", "mostra", "mostrando", "mostrar", "enxerga", "enxergando", "enxergar", "visualiza", "visualizar", "tela", "telas", "imagem", "imagens", "monitor", "monitores", "desktop", "area de trabalho", "janela", "janelas", "screen", "display", "descreve", "descreva", "descrevendo", "identifica", "identificar", "analisa", "analisar", "le", "ler", "leia"]
        logger.info("[VISUAL_ROUTER] Inicializado - keywords=%d", len(self.VISUAL_KEYWORDS))

    @property
    def vision_mode_enabled(self) -> bool:
        return self._vision_enabled

    def set_vision_mode(self, enabled: bool) -> None:
        self._vision_enabled = bool(enabled)
        logger.info("[VISUAL_ROUTER] Modo visão: %s", "ON" if enabled else "OFF")

    def set_vision_enabled(self, enabled: bool) -> None:
        old_state = self._vision_enabled
        self._vision_enabled = bool(enabled)
        logger.info("[VISUAL_ROUTER] Estado alterado - old=%s new=%s", old_state, self._vision_enabled)
        if self._vision_enabled:
            logger.info("[VISUAL_ROUTER] [OK] Modo visao: ON")
        else:
            logger.info("[VISUAL_ROUTER] [OFF] Modo visao: OFF")

    def detect_intent(self, text: str) -> VisualIntent:
        text_clean = self._normalize_text(text)
        if not text_clean:
            return VisualIntent.NONE
        if FuzzyCommandMatcher.is_negative_command(text_clean, "open_web"):
            logger.info("[VISUAL_ROUTER] Intent detectado - NONE (negacao de acao)")
            return VisualIntent.NONE
        if LOCATE_RE.search(text_clean):
            # "Onde está o botão X na tela?" é do locate_on_screen (Gemini): pelo pipeline visual antigo a Kira ligava o
            # compartilhamento de tela e falava em paralelo ("Não consegui alterar o modo visão").
            logger.info("[VISUAL_ROUTER] Intent detectado - NONE (onde esta: locate_on_screen)")
            return VisualIntent.NONE
        if CREATE_IMAGE_RE.search(text_clean):
            logger.info("[VISUAL_ROUTER] Intent detectado - NONE (pedido de criar imagem)")
            return VisualIntent.NONE
        for keyword in self.VISUAL_QUESTION_KEYWORDS:
            if self._matches_keyword(text_clean, keyword):
                logger.info("[VISUAL_ROUTER] Intent detectado - QUESTION keyword='%s'", keyword)
                return VisualIntent.QUESTION
        for keyword in self.VISUAL_ACTION_KEYWORDS:
            if self._matches_keyword(text_clean, keyword):
                logger.info("[VISUAL_ROUTER] Intent detectado - ACTION keyword='%s'", keyword)
                return VisualIntent.ACTION
        for keyword in self.VISUAL_KEYWORDS:
            if self._matches_keyword(text_clean, keyword):
                logger.info("[VISUAL_ROUTER] Intent detectado - QUESTION (keyword='%s')", keyword)
                return VisualIntent.QUESTION
        logger.info("[VISUAL_ROUTER] Intent detectado - NONE (nenhuma keyword visual)")
        return VisualIntent.NONE

    def is_visual_question(self, text: str) -> bool:
        return self.detect_intent(text) == VisualIntent.QUESTION

    def is_visual_query_text(self, text: str) -> bool:
        text_clean = self._normalize_text(text)
        if not text_clean:
            return False
        query_found = any(re.search(pattern, text_clean) for pattern in self.VISION_QUERY_PATTERNS)
        if not query_found:
            for keyword in self.VISUAL_QUESTION_KEYWORDS:
                if self._matches_keyword(text_clean, keyword):
                    query_found = True
                    break
        activation_reference = any(re.search(pattern, text_clean) for pattern in self.VISION_CONTROL_PATTERNS) or any(
            re.search(pattern, text_clean) for pattern in self.VISION_ACTIVATION_REFERENCE_PATTERNS
        )
        if query_found and activation_reference:
            logger.info("VISION_MIXED_INTENT_DETECTED text=%s", text_clean[:180])
            logger.info("VISION_QUERY_WINS_OVER_COMMAND")
            logger.info("VISION_COMMAND_INTENT_IGNORED_REASON=query_present")
        if query_found:
            return True
        return False

    def route(self, text: str) -> tuple[VisualIntent, str | None]:
        intent = self.detect_intent(text)
        if detect_creation_task_command(text):
            logger.info("CREATION_TASK_BROWSER_ROUTE_BLOCKED reason=creation_task")
            return VisualIntent.NONE, None
        if intent is VisualIntent.NONE:
            logger.info("[VISUAL_ROUTER] Comando nao visual - seguindo fluxo padrao")
            return intent, None
        if not self.vision_mode_enabled:
            logger.warning("[VISUAL_ROUTER] Comando visual bloqueado - visao desligada - intent=%s", intent.value)
            return intent, self.VISION_DISABLED_MESSAGE
        logger.info("[VISUAL_ROUTER] Comando visual permitido - intent=%s", intent.value)
        return intent, None

    @classmethod
    def detect_vision_control(cls, text: str) -> bool | None:
        t = cls._normalize_text(text)
        if any(re.search(pattern, t) for pattern in cls.VISION_QUERY_PATTERNS):
            if any(re.search(pattern, t) for pattern in cls.VISION_CONTROL_PATTERNS):
                logger.info("VISION_MIXED_INTENT_DETECTED text=%s", t[:180])
                logger.info("VISION_QUERY_WINS_OVER_COMMAND")
                logger.info("VISION_COMMAND_INTENT_IGNORED_REASON=query_present")
            logger.info("VISION_QUERY_NOT_TREATED_AS_COMMAND text=%s", t[:180])
            return None
        for i, pattern in enumerate(cls.VISION_CONTROL_PATTERNS):
            if re.search(pattern, t):
                logger.info("VISION_COMMAND_INTENT_DETECTED action=%s", "start" if i == 0 else "stop")
                return i == 0
        return None

    @classmethod
    def is_vision_status_query(cls, text: str) -> bool:
        t = cls._normalize_text(text)
        return any(re.search(pattern, t) for pattern in cls.VISION_STATUS_PATTERNS)

    @staticmethod
    def _normalize_text(text: str) -> str:
        raw = str(text or "").strip().lower()
        normalized = unicodedata.normalize("NFKD", raw)
        without_accents = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        return re.sub(r"[^\w\s]", " ", " ".join(without_accents.split()))

    @classmethod
    def _matches_keyword(cls, normalized_text: str, keyword: str) -> bool:
        k = cls._normalize_text(keyword)
        if "+" in k:
            return k in normalized_text
        return re.search(rf"(?<!\w){re.escape(k)}(?!\w)", normalized_text) is not None


__all__ = ["VisualIntent", "VisualRouter"]
