from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

DOMAIN_UI_CONTROL = "DOMAIN_UI_CONTROL"
DOMAIN_SESSION_CONTROL = "DOMAIN_SESSION_CONTROL"
DOMAIN_SYSTEM_CONTROL = "DOMAIN_SYSTEM_CONTROL"
DOMAIN_UNKNOWN = "DOMAIN_UNKNOWN"


@dataclass(frozen=True)
class UiSessionIntent:
    domain: str
    intent: str
    target: str | None = None
    desired_state: bool | None = None
    confidence: float = 0.0
    normalized_command: str = ""


def normalize_text(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    lowered = stripped.lower()
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _config_target(normalized: str) -> str:
    """"Abre as configurações na parte de economia" -> config:economia (a sub-aba abre junto, 2026-11-07)."""
    for terms, sub in ((("econom",), "economia"), (("memoria", "memória"), "memoria"), (("personalidade",), "personalidade"),
                       (("aparencia", "aparência", "visual"), "aparencia"), (("acesso remoto", "remoto"), "remoto")):
        if _has_any(normalized, terms):
            return f"config:{sub}"
    return "config"


def parse_ui_session_intent(command: str) -> UiSessionIntent:
    normalized = normalize_text(command)
    if any(
        phrase in normalized
        for phrase in (
            "status do sistema",
            "estado do sistema",
            "painel de status",
            "status da kira",
            "estado da kira",
            "situacao do sistema",
        )
    ):
        logger.info("SYSTEM_STATUS_BLOCKED_OPEN_TAB reason=status_panel_phrase")
        return UiSessionIntent(
            domain=DOMAIN_UNKNOWN,
            intent="system_status_panel",
            target="session_status",
            confidence=0.99,
            normalized_command=normalized,
        )
    navigation_terms = (
        "abre",
        "abrir",
        "vai para",
        "ir para",
        "mostra",
        "mostrar",
        "volta para",
        "voltar para",
        "abre o",
        "abre a",
    )
    tab_context_terms = ("aba", "painel", "guia", "secao", "seção", "tela")

    system_terms = ("pc", "computador", "windows", "sistema")
    sys_audio_terms = ("volume", "som", "mudo", "silencia", "silenciar")
    if _has_any(normalized, sys_audio_terms) and _has_any(normalized, system_terms):
        return UiSessionIntent(
            domain=DOMAIN_SYSTEM_CONTROL,
            intent="system_audio",
            confidence=0.98,
            normalized_command=normalized,
        )

    mic_terms = ("microfone", "microfono", "mic")
    cam_terms = ("camera", "webcam")
    screen_terms = ("visao", "tela", "compartilhamento", "screen", "modo visao")
    on_terms = ("liga", "ativ", "reativ", "desmuta", "tirar do mudo")
    off_terms = ("desliga", "desativ", "muta", "silencia", "parar")

    toggle_terms = ("alterna", "compartilha", "troca")

    def desired_state_for(text: str) -> bool | None:
        if _has_any(text, off_terms):
            return False
        if _has_any(text, on_terms):
            return True
        return None

    # Sem verbo de ligar/desligar/alternar não é comando: "o que você tá vendo na minha tela?" virava "alternar a
    # visão" e desligava o compartilhamento (sessão de 2026-10-24 11:13).
    is_command = desired_state_for(normalized) is not None or _has_any(normalized, toggle_terms)
    if _has_any(normalized, mic_terms) and is_command:
        return UiSessionIntent(
            domain=DOMAIN_SESSION_CONTROL,
            intent="session_mic",
            target="mic",
            desired_state=desired_state_for(normalized),
            confidence=0.96,
            normalized_command=normalized,
        )
    if _has_any(normalized, cam_terms) and is_command:
        return UiSessionIntent(
            domain=DOMAIN_SESSION_CONTROL,
            intent="session_camera",
            target="webcam",
            desired_state=desired_state_for(normalized),
            confidence=0.95,
            normalized_command=normalized,
        )
    if _has_any(normalized, screen_terms) and is_command:
        return UiSessionIntent(
            domain=DOMAIN_SESSION_CONTROL,
            intent="session_screen",
            target="vision_mode",
            desired_state=desired_state_for(normalized),
            confidence=0.95,
            normalized_command=normalized,
        )

    music_terms = ("musica", "música", "player", "playlist", "faixa")
    if "aba" in normalized:
        if _has_any(normalized, ("whatsapp", "zap", "wpp")):
            return UiSessionIntent(
                domain=DOMAIN_UI_CONTROL,
                intent="open_tab",
                target="whatsapp",
                confidence=0.99,
                normalized_command=normalized,
            )
        if _has_any(normalized, ("musica", "música", "player", "playlist")):
            return UiSessionIntent(
                domain=DOMAIN_UI_CONTROL,
                intent="open_tab",
                target="music",
                confidence=0.99,
                normalized_command=normalized,
            )
        if _has_any(normalized, ("navegador", "browser", "internet")):
            return UiSessionIntent(
                domain=DOMAIN_UI_CONTROL,
                intent="open_tab",
                target="browser",
                confidence=0.99,
                normalized_command=normalized,
            )
        if _has_any(normalized, ("config", "configuracao", "configuracoes", "ajustes")):
            return UiSessionIntent(
                domain=DOMAIN_UI_CONTROL,
                intent="open_tab",
                target=_config_target(normalized),
                confidence=0.99,
                normalized_command=normalized,
            )
        if _has_any(normalized, ("sistema", "telemetria", "status do sistema", "painel do sistema")):
            return UiSessionIntent(
                domain=DOMAIN_UI_CONTROL,
                intent="open_tab",
                target="system",
                confidence=0.99,
                normalized_command=normalized,
            )
        if _has_any(normalized, ("kira", "principal", "inicio", "home", "painel principal")):
            return UiSessionIntent(
                domain=DOMAIN_UI_CONTROL,
                intent="open_tab",
                target="home",
                confidence=0.99,
                normalized_command=normalized,
            )

    if _has_any(normalized, music_terms):
        music_action = None
        if _has_any(normalized, ("pausa", "pausar")):
            music_action = "pause"
        elif _has_any(normalized, ("proxima", "próxima", "avanca", "avançar", "pula")):
            music_action = "next"
        elif _has_any(normalized, ("anterior", "volta", "voltar", "retrocede")):
            music_action = "previous"
        elif _has_any(normalized, ("para", "parar", "stop")):
            music_action = "stop"
        elif _has_any(normalized, ("toca", "tocar", "continua", "continuar", "retoma", "retomar", "play")):
            music_action = "play"
        if music_action:
            return UiSessionIntent(
                domain=DOMAIN_UI_CONTROL,
                intent="music_control",
                target=music_action,
                confidence=0.94,
                normalized_command=normalized,
            )

    if (
        _has_any(normalized, navigation_terms) or _has_any(normalized, tab_context_terms)
    ) and _has_any(
        normalized,
        (
            "aba",
            "config",
            "configuracoes",
            "whatsapp",
            "painel",
            "kira",
            "musica",
            "navegador",
            "browser",
            "inicio",
            "home",
            "sistema",
        ),
    ):
        tab_target = None
        if _has_any(normalized, ("whatsapp", "zap", "wpp")):
            tab_target = "whatsapp"
        elif _has_any(normalized, ("musica", "música", "player", "playlist")):
            tab_target = "music"
        elif _has_any(normalized, ("navegador", "browser", "internet")):
            tab_target = "browser"
        elif _has_any(normalized, ("config", "configuracao", "configuracoes", "ajustes")):
            tab_target = _config_target(normalized)
        elif _has_any(normalized, ("sistema", "telemetria", "status do sistema", "painel do sistema")):
            tab_target = "system"
        elif _has_any(normalized, ("kira", "principal", "inicio", "home", "painel principal")):
            tab_target = "home"
        if tab_target:
            return UiSessionIntent(
                domain=DOMAIN_UI_CONTROL,
                intent="open_tab",
                target=tab_target,
                confidence=0.95,
                normalized_command=normalized,
            )

    if _has_any(normalized, ("aba", "config", "whatsapp", "kira", "painel", "musica", "navegador", "browser")):
        return UiSessionIntent(
            domain=DOMAIN_UI_CONTROL,
            intent="open_tab",
            confidence=0.7,
            normalized_command=normalized,
        )

    return UiSessionIntent(
        domain=DOMAIN_UNKNOWN,
        intent="unknown",
        confidence=0.0,
        normalized_command=normalized,
    )
