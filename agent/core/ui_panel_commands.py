"""Deterministic local parser for Kira UI panel visibility commands."""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger("ui_panel_commands")

PanelName = Literal["system_vitals", "session_status", "api_identity", "transcription", "all"]
PanelAction = Literal["show", "hide", "toggle", "clarify"]

CONTEXT_TTL_SECONDS = 60.0
UI_PANEL_TEXT_ONLY_SOURCES = {"ui_panel", "ui_panel_command", "panel_visibility"}


@dataclass(slots=True)
class UiPanelCommand:
    action: PanelAction
    panel: PanelName
    confidence: float
    normalized_text: str
    source: str | None = None

    @property
    def matched(self) -> bool:
        return True

    @property
    def panels(self) -> list[str]:
        if self.panel == "all":
            return ["system_vitals", "session_status", "api_identity", "transcription"]
        return [self.panel]

    @property
    def reply(self) -> str:
        return build_ui_panel_notice(self.action, self.panel)

    def to_dict(self) -> dict[str, object]:
        return {
            "matched": True,
            "action": self.action,
            "panel": self.panel,
            "panels": self.panels,
            "normalized_text": self.normalized_text,
            "confidence": self.confidence,
            "reply": self.reply,
            "source": self.source,
        }

    def get(self, key: str, default: object = None) -> object:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]


UI_PANEL_REGISTRY: dict[str, dict[str, object]] = {
    "system_vitals": {
        "title": "sistema vital",
        "frontend_id": "kira-vitals-panel",
        "aliases": (
            "sistema vital",
            "sistemas vitais",
            "painel vital",
            "vital",
            "vitais",
            "cpu",
            "ram",
            "cpu e ram",
            "cpu e memoria",
            "cpu memoria",
            "memoria ram",
            "painel de cpu",
            "painel de memoria",
            "painel de memoria ram",
            "vitals",
            "uso do sistema",
            "uso do pc",
            "recursos do pc",
            "desempenho",
            "consumo do sistema",
            "sistema vitau",
            "sistemal vital",
            "sistemaliza",
            "sitema vital",
            "sistema vita",
            "status vital",
        ),
    },
    "session_status": {
        "title": "status do sistema",
        "frontend_id": "kira-session-status-panel",
        "aliases": (
            "status do sistema",
            "status sistema",
            "status",
            "painel de status",
            "painel do status",
            "status da kira",
            "estado do sistema",
            "estado da kira",
            "situacao do sistema",
            "status da sessao",
            "saude do sistema",
            "cerebro",
            "livekit",
            "api",
            "voz api",
            "painel da direita",
            "status geral",
            "estatus do sistema",
            "statos do sistema",
            "cerebro livekit voz api whatsapp",
        ),
    },
    "api_identity": {
        "title": "APIs Ativas",
        "frontend_id": "kira-session-status-panel",
        "aliases": (
            "apis ativas",
            "api ativas",
            "api ativa",
            "as apis ativas",
            "as api ativa",
            "painel de ativos",
            "paineis ativos",
            "painel ativo",
            "status ativos",
            "sistema ativo",
            "a pis ativas",
            "pias ativas",
            "peas ativos",
            "peas ativas",
            "pais ativos",
            "pais ativas",
            "pis ativos",
            "pis ativas",
            "chaves ativas",
            "credenciais ativas",
            "apis do sistema",
            "painel apis ativas",
            "painel de apis ativas",
            "painel das apis",
            "painel de api",
            "painel de apis",
            "painel do gemini",
            "painel do livekit",
            "apis",
            "api identity",
            "identidade de api",
            "api livekit gemini",
            "livekit gemini",
            "gemini livekit",
            "gemini e livekit",
            "livekit e gemini",
        ),
    },
    "transcription": {
        "title": "transcricao",
        "frontend_id": "kira-transcript-panel",
        "aliases": (
            "transcricao",
            "painel de transcricao",
            "painel da transcricao",
            "historico",
            "historico da conversa",
            "conversa",
            "chat",
            "chat da esquerda",
            "legenda",
            "texto da conversa",
            "transcrissao",
            "transcrisao",
            "transcrecao",
        ),
    },
    "all": {
        "title": "paineis",
        "frontend_id": "",
        "aliases": (
            "tudo",
            "todos",
            "todos os paineis",
            "todos os containers",
            "containers",
            "paineis da tela",
            "interface toda",
            "tudo da tela",
            "todos os cards",
            "todos da tela",
        ),
    },
}

_HIDE_PATTERNS = (
    "apagar da tela",
    "tira da tela",
    "tirar da tela",
    "deixa invisivel",
    "nao mostrar",
    "nao mostre",
    "some com",
    "desabilitar",
    "desabilita",
    "desativar",
    "desativa",
    "minimizar",
    "minimiza",
    "recolher",
    "recolhe",
    "esconder",
    "esconde",
    "esconda",
    "ocultar",
    "oculta",
    "oculte",
    "ocutar",
    "ocuta",
    "o cuta",
    "o cutar",
    "cuta",
    "oculto",
    "fechar",
    "fecha",
    "remover",
    "remove",
    "remova",
    "sumir",
    "suma",
    "some",
    "tirar",
    "tira",
)

_SHOW_PATTERNS = (
    "colocar de volta",
    "coloca de volta",
    "coloca na tela",
    "botar na tela",
    "bota na tela",
    "trazer de volta",
    "deixa eu ver",
    "quero ver",
    "habilitar",
    "habilita",
    "restaurar",
    "restaura",
    "maximizar",
    "maximiza",
    "expandir",
    "expande",
    "mostrar",
    "mostra",
    "mostre",
    "exibir",
    "exibe",
    "exiba",
    "aparecer",
    "aparece",
    "apareca",
    "ativar",
    "ativa",
    "ativa painel",
    "ativar painel",
    "abrir",
    "abre",
    "trazer",
    "traz",
    "voltar",
    "volta",
    # "coloca" puro REMOVIDO: colidia com comandos de mídia/navegador ("coloca X no
    # YouTube") abrindo o painel de música por engano. Os usos legítimos de painel
    # seguem cobertos por "coloca de volta"/"coloca na tela" (no topo de _SHOW_PATTERNS).
    "cade",
    "mostar",
    "mosta",
    "deixa visivel",
    "deixe visivel",
)

_TOGGLE_PATTERNS = (
    "alternar",
    "alterna",
    "toggle",
)

_SYSTEM_STATUS_PHRASES = (
    "status do sistema",
    "estado do sistema",
    "painel de status",
    "status da kira",
    "estado da kira",
    "situacao do sistema",
)

_CONTEXT_REFERENCE_PATTERNS = (
    "ele",
    "ela",
    "esse",
    "essa",
    "isso",
    "de novo",
    "novamente",
    "volta ele",
    "mostra ele",
    "oculta ele",
    "abre ele",
    "fecha ele",
    "pode ocultar ele de novo",
    "pode mostrar ele de novo",
    "faz isso de novo",
)

_TEXT_FIXES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), replacement)
    for pattern, replacement in (
        (r"\bo\s+cuta\b", "oculta"),
        (r"\bo\s+cutar\b", "ocultar"),
        (r"\bcuta\b", "oculta"),
        (r"\bnao\s+mostrar\b", "ocultar"),
        (r"\bnao\s+mostre\b", "ocultar"),
        (r"\bocuta\b", "oculta"),
        (r"\boculte\b", "ocultar"),
        (r"\bocute\b", "oculta"),
        (r"\bocuto\b", "oculta"),
        (r"\bpeas\s+ativos\b", "apis ativas"),
        (r"\bpeas\s+ativas\b", "apis ativas"),
        (r"\bpais\s+ativos\b", "apis ativas"),
        (r"\bpa[ií]s\s+ativas\b", "apis ativas"),
        (r"\bpais\s+ativas\b", "apis ativas"),
        (r"\bpis\s+ativos\b", "apis ativas"),
        (r"\bpis\s+ativas\b", "apis ativas"),
        (r"\bpanel\s+de\s+ativos\b", "painel de ativos"),
        (r"\ba\s+pis\s+ativas\b", "apis ativas"),
        (r"\bpias\s+ativas\b", "apis ativas"),
        (r"\bapi\s+ativas\b", "apis ativas"),
        # "oculta a PII ativos" (2026-10-31): o reconhecimento junta o "a" de "as APIs" e troca o gênero
        (r"\b(?:a\s+)?(?:pii|piis|apii|api|apis)\s+ativ[oa]s\b", "apis ativas"),
        # "Quiero cutar, o panes de ativos" (2026-10-31)
        (r"\bcutar\b", "ocultar"),
        (r"\bpa(?:i|ne|nel|nes)s?\s+de\s+ativ[oa]s\b", "apis ativas"),
        (r"\boculto(?=\s+(o|a|os|as|status|sistema|cpu|ram|historico|transcricao))", "oculta"),
        (r"\bsistemaliza\b", "sistema vital"),
        (r"\bsistemal\s+vital\b", "sistema vital"),
        (r"\bsitema\s+vital\b", "sistema vital"),
        (r"\bsistema\s+vita\b", "sistema vital"),
        (r"\bsistema\s+vitau\b", "sistema vital"),
        (r"\btranscrissao\b", "transcricao"),
        (r"\btranscrisao\b", "transcricao"),
        (r"\btranscrecao\b", "transcricao"),
        (r"\btrascricao\b", "transcricao"),
        (r"\btranscric?ao\b", "transcricao"),
        (r"\bestatus\s+(?:do\s+)?sistema\b", "status do sistema"),
        (r"\bstatos\s+(?:do\s+)?sistema\b", "status do sistema"),
        (r"\bstatus\s+sistema\b", "status do sistema"),
        (r"\bstatus\s+da\s+kira\b", "status do sistema"),
        (r"\bestado\s+da\s+kira\b", "estado do sistema"),
        (r"\bmosta(?:r)?\b", "mostra"),
    )
)

_last_ui_panel_command: dict[str, object] = {}


def _strip_accents(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    )


def normalize_ui_panel_command_text(text: str) -> str:
    normalized = _strip_accents(str(text or "").lower().strip())
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    for pattern, replacement in _TEXT_FIXES:
        normalized = pattern.sub(replacement, normalized)
    normalized = re.sub(r"\b(kira|pira|queira|cara)\b", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _iter_phrase_matches(text: str, phrases: tuple[str, ...]) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for phrase in sorted(phrases, key=len, reverse=True):
        for match in re.finditer(rf"(?<!\w){re.escape(phrase)}(?!\w)", text):
            matches.append((match.start(), match.end(), phrase))
    matches.sort(key=lambda item: (item[0], item[1]))
    return matches


def _find_actions(text: str) -> list[tuple[int, str, str]]:
    actions: list[tuple[int, str, str]] = []
    for start, _end, phrase in _iter_phrase_matches(text, _HIDE_PATTERNS):
        actions.append((start, "hide", phrase))
    for start, _end, phrase in _iter_phrase_matches(text, _SHOW_PATTERNS):
        actions.append((start, "show", phrase))
    for start, _end, phrase in _iter_phrase_matches(text, _TOGGLE_PATTERNS):
        actions.append((start, "toggle", phrase))
    actions.sort(key=lambda item: item[0])
    return actions


def _match_panels(text: str) -> list[tuple[int, PanelName, str]]:
    hits: list[tuple[int, PanelName, str]] = []
    for panel, cfg in UI_PANEL_REGISTRY.items():
        aliases = tuple(str(alias) for alias in cfg.get("aliases", ()))
        for start, _end, alias in _iter_phrase_matches(text, aliases):
            hits.append((start, panel, alias))  # type: ignore[arg-type]
    hits.sort(key=lambda item: item[0])
    return hits


def _select_panel_for_action(
    panels: list[tuple[int, PanelName, str]],
    action_pos: int,
) -> PanelName | None:
    if not panels:
        return None
    after_action = [item for item in panels if item[0] >= action_pos]
    if after_action:
        after_action.sort(key=lambda item: (item[0], -len(item[2])))
        return after_action[0][1]
    return panels[-1][1]


def _has_context_reference(text: str) -> bool:
    return any(_contains_phrase(text, phrase) for phrase in _CONTEXT_REFERENCE_PATTERNS)


def reset_ui_panel_context() -> None:
    _last_ui_panel_command.clear()


def remember_ui_panel_command(cmd: UiPanelCommand) -> None:
    _last_ui_panel_command.clear()
    _last_ui_panel_command.update(
        {
            "panel": cmd.panel,
            "action": cmd.action,
            "timestamp": time.monotonic(),
        }
    )


def _resolve_context_panel(text: str) -> tuple[PanelName | None, PanelAction | None]:
    if not _has_context_reference(text):
        return None, None
    logger.info('UI_PANEL_CONTEXT_REFERENCE_DETECTED text="%s"', text[:180])
    if not _last_ui_panel_command:
        logger.info("UI_PANEL_CONTEXT_MISSING reason=no_last_panel")
        return None, None
    age_ms = int((time.monotonic() - float(_last_ui_panel_command.get("timestamp") or 0.0)) * 1000)
    if age_ms > int(CONTEXT_TTL_SECONDS * 1000):
        logger.info("UI_PANEL_CONTEXT_EXPIRED age_ms=%s", age_ms)
        return None, None
    panel = str(_last_ui_panel_command.get("panel") or "").strip()
    action = str(_last_ui_panel_command.get("action") or "").strip()
    if panel not in UI_PANEL_REGISTRY or action not in {"show", "hide"}:
        logger.info("UI_PANEL_CONTEXT_MISSING reason=invalid_last_panel")
        return None, None
    logger.info("UI_PANEL_CONTEXT_RESOLVED panel=%s age_ms=%s", panel, age_ms)
    return panel, action  # type: ignore[return-value]


def _raw_leading_negative_correction(raw_text: str) -> bool:
    folded = _strip_accents(str(raw_text or "").lower())
    return re.match(r"^\s*nao\s*[,;:\-]\s*", folded) is not None


def _correction_allowed(raw_text: str, normalized: str, action_pos: int, action_count: int) -> bool:
    prefix = normalized[: max(action_pos, 0)].strip()
    if _raw_leading_negative_correction(raw_text):
        return True
    if "nao" not in prefix:
        return False
    if "quis dizer" in prefix or "eu quis dizer" in prefix:
        return True
    if action_count > 1 and any(marker in prefix for marker in (" quero ", " e pra ", " e para ", " era ", " quis ")):
        return True
    return False


def _is_real_negation(normalized: str, action_phrase: str, action_pos: int, correction: bool) -> bool:
    if correction:
        return False
    prefix = normalized[: max(action_pos, 0)].strip()
    suffix = normalized[action_pos:].strip()
    if re.search(r"\bnao\s+mexe\b", normalized):
        return True
    if re.match(rf"^nao\s+(quero\s+)?{re.escape(action_phrase)}\b", suffix):
        return True
    if re.search(rf"\bnao\s+{re.escape(action_phrase)}\b", normalized):
        return True
    if re.search(rf"\bnao\s+quero\s+{re.escape(action_phrase)}\b", normalized):
        return True
    if re.search(rf"\bnao\s+e\s+pra\s+{re.escape(action_phrase)}\b", normalized):
        return True
    if prefix.startswith("nao") and "quis dizer" not in prefix and "quero ocultar" not in normalized and "quero mostrar" not in normalized:
        return True
    return False


def build_ui_panel_notice(action: str, panel: str) -> str:
    if panel == "all":
        return "Interface: pain\u00e9is vis\u00edveis." if action == "show" else "Interface: pain\u00e9is ocultos."
    if panel == "system_vitals":
        return "Interface: sistema vital vis\u00edvel." if action == "show" else "Interface: sistema vital oculto."
    if panel == "session_status":
        if action == "toggle":
            return "Interface: status do sistema alternado."
        return "Interface: status do sistema vis\u00edvel." if action == "show" else "Interface: status do sistema oculto."
    if panel == "transcription":
        return "Interface: transcri\u00e7\u00e3o vis\u00edvel." if action == "show" else "Interface: transcri\u00e7\u00e3o oculta."
    return "Interface: painel vis\u00edvel." if action == "show" else "Interface: painel oculto."


def is_ui_panel_text_only_source(source: str | None) -> bool:
    return str(source or "").strip().lower() in UI_PANEL_TEXT_ONLY_SOURCES


def command_to_dict(command: UiPanelCommand | dict | None) -> dict | None:
    if command is None:
        return None
    if isinstance(command, UiPanelCommand):
        return command.to_dict()
    if isinstance(command, dict):
        return command
    return None


def looks_like_panel_command_candidate(text: str) -> bool:
    normalized = normalize_ui_panel_command_text(text)
    if not normalized:
        return False
    actions = _find_actions(normalized)
    panels = _match_panels(normalized)
    if actions and panels:
        return True
    action_like = bool(re.search(r"\b(?:ocultar|oculta|oculte|ocuta|ocutar|o cuta|o cutar|cuta|esconder|esconda|tirar|tira|remover|remova|fechar|fecha|sumir|suma|desativar|desativa|mostrar|mostra|mostre|exibir|exibe|exiba|abrir|abre|aparecer|aparece|apareca|ativar|ativa|coloca|bota)\b", normalized))
    panel_like = bool(re.search(r"\b(?:api|apis|peas|pis|pais|pias|gemini|livekit|chaves|credenciais|transcricao|sistema|vital|vitals|cpu|ram|memoria|desempenho|recursos|status)\b", normalized))
    return bool(action_like and panel_like)


def detect_ui_panel_visibility_command(text: str) -> UiPanelCommand | None:
    logger.info('UI_PANEL_AUTOCORRECT_BEGIN original_text="%s"', str(text or "")[:180])
    logger.info('UI_PANEL_PARSER_INPUT raw="%s"', str(text or "")[:180])
    normalized = normalize_ui_panel_command_text(text)
    raw_normalized = re.sub(r"\s+", " ", _strip_accents(str(text or "").lower().strip())).strip()
    logger.info('UI_PANEL_PARSER_NORMALIZED normalized="%s"', normalized[:180])
    if normalized != raw_normalized:
        logger.info(
            'LOCAL_COMMAND_STT_AUTOCORRECTED raw="%s" normalized="%s"',
            str(text or "")[:180],
            normalized[:180],
        )
    if not normalized:
        logger.info("UI_PANEL_AUTOCORRECT_NO_MATCH reason=empty")
        logger.info("UI_PANEL_PARSER_NOT_MATCHED reason=empty")
        return None
    if re.search(r"\b(?:monitor(?:es)?|bloco\s+de\s+notas|notepad|codex|vs\s+code|vscode)\b", normalized):
        logger.info("UI_PANEL_AUTOCORRECT_NO_MATCH reason=operator_context")
        logger.info("UI_PANEL_PARSER_NOT_MATCHED reason=operator_context")
        return None

    actions = _find_actions(normalized)
    panels = _match_panels(normalized)
    action: PanelAction | None = None
    action_pos = -1
    action_phrase = ""
    if actions:
        action_pos, action_value, action_phrase = actions[-1]
        action = action_value  # type: ignore[assignment]
        logger.info(
            "UI_PANEL_VISIBILITY_VERB_MATCHED raw=%r normalized=%r action=%s",
            str(text or "")[:120],
            action_phrase,
            action,
        )

    panel = _select_panel_for_action(panels, action_pos)
    if panel is None:
        context_panel, context_action = _resolve_context_panel(normalized)
        if context_panel is not None:
            panel = context_panel
            if action is None:
                action = context_action
                action_pos = 0
                action_phrase = str(context_action or "")

    if action is None:
        if panel == "session_status" and any(_contains_phrase(normalized, phrase) for phrase in _SYSTEM_STATUS_PHRASES):
            action = "show"
            action_pos = 0
            action_phrase = "status_panel_default_show"
        else:
            logger.info("UI_PANEL_AUTOCORRECT_NO_MATCH reason=no_visibility_verb")
            logger.info("UI_PANEL_PARSER_NOT_MATCHED reason=no_visibility_verb")
            return None
    if panel is None and re.search(r"\bsistema\b", normalized):
        panel = "system_vitals"
    if panel is None:
        logger.info("UI_PANEL_AUTOCORRECT_NO_MATCH reason=no_panel_alias")
        logger.info("UI_PANEL_PARSER_NOT_MATCHED reason=no_panel_alias")
        return None

    correction = _correction_allowed(text, normalized, action_pos, len(actions))
    if _is_real_negation(normalized, action_phrase, action_pos, correction):
        logger.info("UI_PANEL_AUTOCORRECT_NO_MATCH reason=real_negation")
        logger.info("UI_PANEL_PARSER_NEGATION_BLOCKED reason=real_negation")
        return None
    if correction:
        logger.info("UI_PANEL_PARSER_CORRECTION_ALLOWED action=%s panel=%s", action, panel)

    confidence = 0.99
    if len(panels) != 1:
        confidence = 0.94 if panel != "all" else 0.98
    cmd = UiPanelCommand(
        action=action,
        panel=panel,
        confidence=confidence,
        normalized_text=normalized,
        source=None,
    )
    logger.info("UI_PANEL_PARSER_MATCHED action=%s panel=%s confidence=%.2f", cmd.action, cmd.panel, cmd.confidence)
    logger.info("UI_PANEL_NATURAL_COMMAND_MATCHED")
    logger.info("UI_PANEL_TARGET_MATCHED target=%s", cmd.panel)
    matched_alias = next((alias for _start, panel_name, alias in panels if panel_name == cmd.panel), "")
    if matched_alias:
        logger.info(
            "UI_PANEL_ALIAS_MATCHED raw=%r target=%s",
            str(text or "")[:120],
            cmd.panel,
        )
        logger.info(
            "UI_PANEL_ALIAS_MATCHED raw=%r normalized=%r target=%s action=%s",
            str(text or "")[:120],
            matched_alias,
            cmd.panel,
            cmd.action,
        )
    if confidence < 0.99 or normalized != raw_normalized:
        logger.info("UI_PANEL_FUZZY_MATCHED confidence=%.2f", confidence)
        logger.info("LOCAL_COMMAND_STT_FUZZY_MATCHED")
    logger.info(
        'UI_PANEL_AUTOCORRECT_MATCHED original_text="%s" corrected_action="%s" corrected_targets="%s"',
        str(text or "")[:180],
        cmd.action,
        ",".join(cmd.panels),
    )
    logger.info("UI_PANEL_COMMAND_MATCHED action=%s targets=%s", cmd.action, ",".join(cmd.panels))
    if cmd.panel == "session_status":
        logger.info("SYSTEM_STATUS_GATE_MATCHED action=%s panel_id=session_status", cmd.action)
        logger.info("SYSTEM_STATUS_PANEL_ACTION action=%s panel_id=session_status", cmd.action)
    remember_ui_panel_command(cmd)
    return cmd


def detect_panel_visibility_command(text: str) -> UiPanelCommand | None:
    return detect_ui_panel_visibility_command(text)


def try_execute_panel_command(text: str, *args, **kwargs) -> dict:
    del args, kwargs
    try:
        cmd = detect_ui_panel_visibility_command(text)
        if not cmd:
            return {"handled": False}
        logger.info("UI_PANEL_LEGACY_ROUTE_BLOCKED route=try_execute_panel_command reason=handled_by_ui_panel_gate")
        return {
            "handled": True,
            "action": cmd.action,
            "panel": cmd.panel,
            "panels": cmd.panels,
            "ack_text": cmd.reply,
            "command": cmd.to_dict(),
        }
    except Exception as exc:
        logger.warning("UI_PANEL_COMMAND_INTERCEPT_FAILED_CONTINUE error=%s", exc)
        return {"handled": False}
