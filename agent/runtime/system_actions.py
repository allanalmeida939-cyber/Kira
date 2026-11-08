"""
Motor de ações do sistema - detecta e executa comandos Windows
Intercepta comandos ANTES da IA para execução direta
"""
import os
import re
import logging
import webbrowser
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

from core.command_intent import classify_explicit_command

logger = logging.getLogger(__name__)
_LAST_ACTION_SIGNATURE = ""
_LAST_ACTION_AT = 0.0
_DUPLICATE_WINDOW_SECONDS = 20.0
_LAST_VOLUME_COMMAND_AT = 0.0
_VOLUME_CONTINUATION_SECONDS = 12.0


class ActionIntent:
    """Representa uma intencao de acao detectada"""

    def __init__(self, action_type: str, target: str, params: Dict[str, Any] = None):
        self.action_type = action_type
        self.target = target
        self.params = params or {}


def detect_action_intent(text: str) -> Optional[ActionIntent]:
    """
    Detecta se o texto e um comando de acao do sistema.
    Retorna ActionIntent se detectado, None caso contrario.
    """
    raw_text = str(text or "").strip()
    text_lower = raw_text.lower()
    if not text_lower:
        return None
    if _looks_like_volume_command(text_lower):
        return _detect_volume_intent(text_lower)
    decision = classify_explicit_command(raw_text)
    logger.info(
        "[INTENT] text=%r is_command=%s confidence=%.2f action=system_action_detect reason=%s",
        raw_text[:120],
        decision.is_command,
        decision.confidence,
        decision.reason,
    )
    if not decision.is_command:
        return None

    if any(word in text_lower for word in ["abre", "abrir", "abra", "abre o", "abre a"]):
        return _detect_open_intent(text_lower, raw_text)

    if any(word in text_lower for word in ["fecha", "fechar", "fecha isso", "fecha essa janela"]):
        return ActionIntent("close", "current_window")

    if any(word in text_lower for word in ["navega", "vai para", "acessa"]):
        return _detect_navigate_intent(text_lower, raw_text)

    return None


def _detect_open_intent(text_lower: str, original_text: str) -> ActionIntent:
    """Detecta o que deve ser aberto"""

    sites = {
        "youtube": "https://www.youtube.com",
        "google": "https://www.google.com",
        "gmail": "https://mail.google.com",
        "facebook": "https://www.facebook.com",
        "instagram": "https://www.instagram.com",
        "twitter": "https://twitter.com",
        "x": "https://twitter.com",
        "whatsapp": "https://web.whatsapp.com",
        "netflix": "https://www.netflix.com",
        "spotify": "https://open.spotify.com",
        "twitch": "https://www.twitch.tv",
    }

    for keyword, url in sites.items():
        if keyword in text_lower:
            use_internal = any(tok in text_lower for tok in ["interno", "na kira", "navegador da kira", "browser da kira"])
            action = "navigate_internal" if use_internal else "open_url"
            return ActionIntent(action, url, {"use_internal": use_internal})

    apps = {
        "bloco de notas": "notepad.exe",
        "notepad": "notepad.exe",
        "calculadora": "calc.exe",
        "calc": "calc.exe",
        "paint": "mspaint.exe",
        "cmd": "cmd.exe",
        "prompt": "cmd.exe",
        "powershell": "powershell.exe",
        "explorador": "explorer.exe",
        "explorer": "explorer.exe",
        "arquivos": "explorer.exe",
        "task manager": "taskmgr.exe",
        "gerenciador de tarefas": "taskmgr.exe",
    }

    for keyword, exe in apps.items():
        if keyword in text_lower:
            return ActionIntent("open_app", exe)

    folders = {
        "downloads": str(Path.home() / "Downloads"),
        "documentos": str(Path.home() / "Documents"),
        "imagens": str(Path.home() / "Pictures"),
        "vídeos": str(Path.home() / "Videos"),
        "videos": str(Path.home() / "Videos"),
        "música": str(Path.home() / "Music"),
        "musica": str(Path.home() / "Music"),
        "área de trabalho": str(Path.home() / "Desktop"),
        "area de trabalho": str(Path.home() / "Desktop"),
        "desktop": str(Path.home() / "Desktop"),
    }

    for keyword, path in folders.items():
        if keyword in text_lower:
            return ActionIntent("open_folder", path)

    url_match = re.search(r"(https?://[^\s]+|www\.[^\s]+)", original_text)
    if url_match:
        url = url_match.group(1)
        if not url.startswith("http"):
            url = "https://" + url
        return ActionIntent("open_url", url, {"use_internal": False})

    return ActionIntent("open_unknown", text_lower)


def _looks_like_volume_command(text_lower: str) -> bool:
    global _LAST_VOLUME_COMMAND_AT
    if any(word in text_lower for word in ("volume", "som", "áudio", "audio")):
        return True
    if re.search(r"\b(aumenta|aumentar|subir|sobe|diminui|diminuir|abaixar|abaixa|baixa|mutar|muta|silenciar|desmutar|desmuta)\b", text_lower):
        return True
    if re.fullmatch(r"\s*\d{1,3}\s*(?:%|porcento|por cento)?\s*", text_lower):
        return (time.monotonic() - _LAST_VOLUME_COMMAND_AT) <= _VOLUME_CONTINUATION_SECONDS
    return False


def _detect_volume_intent(text_lower: str) -> ActionIntent:
    """Detecta comandos de volume"""

    if re.search(r"\b(desmutar|desmuta|unmute|tira o mute|desilencia)\b", text_lower):
        return ActionIntent("volume", "unmute")

    if re.search(r"\b(mutar|muta|mute|silencia|silenciar)\b", text_lower):
        return ActionIntent("volume", "mute")

    if re.search(r"\b(aumenta|aumentar|subir|sobe)\b", text_lower):
        return ActionIntent("volume", "increase", {"amount": 10})

    if re.search(r"\b(diminui|diminuir|abaixar|abaixa|baixa)\b", text_lower):
        return ActionIntent("volume", "decrease", {"amount": 10})

    percent_match = re.search(r"(?:volume\s*(?:em|para)?\s*|coloca\s+o\s+volume\s+em\s*)?(\d{1,3})\s*(?:%|porcento|por cento)?", text_lower)
    if percent_match:
        value = int(percent_match.group(1))
        return ActionIntent("volume", "set", {"value": min(100, max(0, value))})

    if "máximo" in text_lower or "maximo" in text_lower or "total" in text_lower:
        return ActionIntent("volume", "set", {"value": 100})

    if "mínimo" in text_lower or "minimo" in text_lower or "zero" in text_lower:
        return ActionIntent("volume", "set", {"value": 0})

    return ActionIntent("volume", "unknown")


def _detect_navigate_intent(text_lower: str, original_text: str) -> ActionIntent:
    """Detecta navegacao no navegador interno"""
    url_match = re.search(r"(https?://[^\s]+|www\.[^\s]+)", original_text)
    if url_match:
        url = url_match.group(1)
        if not url.startswith("http"):
            url = "https://" + url
        return ActionIntent("navigate_internal", url)

    return ActionIntent("navigate_internal", text_lower)


def execute_action(intent: ActionIntent) -> Tuple[bool, str]:
    """
    Executa a acao detectada.
    Retorna (sucesso: bool, mensagem_retorno: str)
    """
    try:
        if intent.action_type == "open_url":
            return _execute_open_url(intent)
        if intent.action_type == "open_app":
            return _execute_open_app(intent)
        if intent.action_type == "open_folder":
            return _execute_open_folder(intent)
        if intent.action_type == "close":
            return _execute_close(intent)
        if intent.action_type == "volume":
            return _execute_volume(intent)
        if intent.action_type == "navigate_internal":
            return _execute_navigate_internal(intent)

        logger.warning("[SYSTEM_ACTION] unknown_action_type type=%s", intent.action_type)
        return False, f"Nao sei como executar: {intent.action_type}"
    except Exception as e:
        logger.error("[SYSTEM_ACTION] execution_failed type=%s error=%s", intent.action_type, e)
        return False, f"Erro ao executar acao: {e}"


def _execute_open_url(intent: ActionIntent) -> Tuple[bool, str]:
    """Abre URL no navegador"""
    url = intent.target
    use_internal = bool(intent.params.get("use_internal", False))

    logger.info("[SYSTEM_ACTION] opening_url url=%s internal=%s", url, str(use_internal).lower())

    if use_internal:
        internal_ok, internal_msg = _execute_navigate_internal(ActionIntent("navigate_internal", url))
        if internal_ok:
            return True, internal_msg

    webbrowser.open(url)
    logger.info("[SYSTEM_ACTION] url_opened url=%s", url)

    domain = url.split("/")[2] if "http" in url else url
    return True, f"Abrindo {domain}"


def _execute_open_app(intent: ActionIntent) -> Tuple[bool, str]:
    """Abre aplicativo Windows"""
    app = intent.target

    logger.info("[SYSTEM_ACTION] opening_app app=%s", app)

    try:
        subprocess.Popen([app], shell=False)
        logger.info("[SYSTEM_ACTION] app_opened app=%s", app)

        app_name = app.replace(".exe", "").title()
        return True, f"Abrindo {app_name}"
    except Exception as e:
        logger.error("[SYSTEM_ACTION] app_open_failed app=%s error=%s", app, e)
        return False, f"Nao consegui abrir {app}"


def _execute_open_folder(intent: ActionIntent) -> Tuple[bool, str]:
    """Abre pasta no Explorer"""
    path = intent.target

    logger.info("[SYSTEM_ACTION] opening_folder path=%s", path)

    if not Path(path).exists():
        logger.warning("[SYSTEM_ACTION] folder_not_found path=%s", path)
        return False, f"Pasta nao encontrada: {path}"

    try:
        if hasattr(os, "startfile"):
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])
        logger.info("[SYSTEM_ACTION] folder_opened path=%s", path)

        folder_name = Path(path).name or "pasta"
        return True, f"Abrindo {folder_name}"
    except Exception as e:
        logger.error("[SYSTEM_ACTION] folder_open_failed path=%s error=%s", path, e)
        return False, "Erro ao abrir pasta"


def _execute_close(intent: ActionIntent) -> Tuple[bool, str]:
    """Fecha janela atual (Alt+F4)"""
    _ = intent
    logger.info("[SYSTEM_ACTION] closing_window")

    try:
        import pyautogui

        pyautogui.hotkey("alt", "f4")
        logger.info("[SYSTEM_ACTION] window_closed")
        return True, "Fechando janela"
    except Exception as e:
        logger.error("[SYSTEM_ACTION] close_failed error=%s", e)
        return False, "Erro ao fechar janela"


def _execute_volume(intent: ActionIntent) -> Tuple[bool, str]:
    """Controla volume do sistema"""
    action = intent.target

    global _LAST_VOLUME_COMMAND_AT
    logger.info("[SYSTEM_ACTION] volume_control action=%s params=%s", action, intent.params)
    logger.info("VOLUME_GATE_MATCHED action=%s params=%s", action, intent.params)

    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        from comtypes import CLSCTX_ALL
        from ctypes import cast, POINTER

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))

        if action == "mute":
            volume.SetMute(1, None)
            logger.info("[SYSTEM_ACTION] volume_muted")
            logger.info("VOLUME_MUTE_OK muted=true")
            _LAST_VOLUME_COMMAND_AT = time.monotonic()
            return True, "Volume silenciado"

        if action == "unmute":
            volume.SetMute(0, None)
            logger.info("[SYSTEM_ACTION] volume_unmuted")
            logger.info("VOLUME_MUTE_OK muted=false")
            _LAST_VOLUME_COMMAND_AT = time.monotonic()
            return True, "Som ativado"

        if action == "increase":
            current = volume.GetMasterVolumeLevelScalar()
            amount = max(1, int(intent.params.get("amount", 10))) / 100.0
            new_level = min(1.0, current + amount)
            volume.SetMasterVolumeLevelScalar(new_level, None)
            logger.info("[SYSTEM_ACTION] volume_increased from=%.2f to=%.2f", current, new_level)
            logger.info("VOLUME_STEP_OK direction=increase from=%.0f to=%.0f", current * 100.0, new_level * 100.0)
            _LAST_VOLUME_COMMAND_AT = time.monotonic()
            return True, "Volume aumentado"

        if action == "decrease":
            current = volume.GetMasterVolumeLevelScalar()
            amount = max(1, int(intent.params.get("amount", 10))) / 100.0
            new_level = max(0.0, current - amount)
            volume.SetMasterVolumeLevelScalar(new_level, None)
            logger.info("[SYSTEM_ACTION] volume_decreased from=%.2f to=%.2f", current, new_level)
            logger.info("VOLUME_STEP_OK direction=decrease from=%.0f to=%.0f", current * 100.0, new_level * 100.0)
            _LAST_VOLUME_COMMAND_AT = time.monotonic()
            return True, "Volume diminuido"

        if action == "set":
            value = int(intent.params.get("value", 50))
            value = min(100, max(0, value))
            level = value / 100.0
            volume.SetMasterVolumeLevelScalar(level, None)
            logger.info("[SYSTEM_ACTION] volume_set level=%s%%", value)
            logger.info("VOLUME_SET_PERCENT_OK value=%s", value)
            _LAST_VOLUME_COMMAND_AT = time.monotonic()
            return True, f"Volume em {value}%"

        return False, "Comando de volume nao reconhecido"

    except ImportError:
        logger.error("[SYSTEM_ACTION] pycaw_not_installed")
        logger.error("VOLUME_COMMAND_FAILED reason=pycaw_not_installed")
        return False, "Biblioteca de controle de audio nao instalada. Execute: pip install pycaw"
    except Exception as e:
        logger.error("[SYSTEM_ACTION] volume_control_failed error=%s", e)
        logger.error("VOLUME_COMMAND_FAILED reason=%s", e)
        return False, f"Erro no controle de volume: {e}"


def _execute_navigate_internal(intent: ActionIntent) -> Tuple[bool, str]:
    """Navega no navegador interno da Kira"""
    url = intent.target

    logger.info("[SYSTEM_ACTION] internal_navigate url=%s", url)

    try:
        from browser.kira_browser import get_browser_instance

        browser = get_browser_instance()
        browser.navigate_to(url)

        logger.info("[SYSTEM_ACTION] internal_navigation_success url=%s", url)
        return True, f"Navegando para {url}"
    except Exception as e:
        logger.error("[SYSTEM_ACTION] internal_navigation_failed error=%s", e)
        webbrowser.open(url)
        return True, "Abrindo no navegador externo"


def intercept_and_execute(user_input: str) -> Tuple[bool, Optional[str]]:
    """
    Intercepta input do usuario e executa acao se detectada.

    Returns:
        (foi_executado: bool, resposta_para_usuario: Optional[str])

    Se foi_executado=True, a resposta ja foi gerada e nao deve ir para IA.
    Se foi_executado=False, continuar fluxo normal (enviar para IA).
    """
    global _LAST_ACTION_SIGNATURE, _LAST_ACTION_AT
    raw_input = str(user_input or "").strip()
    if not raw_input:
        return False, None
    lowered = raw_input.lower()
    if _looks_like_volume_command(lowered):
        intent = _detect_volume_intent(lowered)
        logger.info("[SYSTEM_ACTION] action_detected type=%s target=%s", intent.action_type, intent.target)
        success, message = execute_action(intent)
        if success:
            logger.info("[SYSTEM_ACTION] action_executed message=%s", message)
        else:
            logger.warning("[SYSTEM_ACTION] action_failed message=%s", message)
        return True, message
    decision = classify_explicit_command(raw_input)
    logger.info(
        "[INTENT] text=%r is_command=%s confidence=%.2f action=system_action_execute reason=%s",
        raw_input[:120],
        decision.is_command,
        decision.confidence,
        decision.reason,
    )
    if not decision.is_command:
        logger.debug("[INTERCEPT] conversational_text_ignored input=%s", raw_input[:50])
        return False, None

    try:
        browser_cmd = any(word in lowered for word in ("site", "google", "internet", "navegador", "pesquisa", "pesquisar"))
        if browser_cmd:
            # HOTFIX 3.5 — se o Browser Agent novo ja assumiu o turno, NAO emitir
            # status/confirmacao legada ("Fechado, chefe...") nem abrir nada.
            try:
                from agent.core.browser_turn_context import browser_agent_owns_turn
                if browser_agent_owns_turn():
                    logger.info("PC_CONTROL_OPEN_WEB_BLOCKED_BY_BROWSER_AGENT source=system_actions_intercept")
                    logger.info("BROWSER_TURN_EXTRA_REPLY_DROPPED source=system_actions_intercept")
                    return True, None
            except Exception:
                pass
            logger.info("[INTERCEPT] browser_agent_command_detected")
            signature = f"browser-agent:{raw_input.lower()[:80]}"
            now = time.monotonic()
            if signature == _LAST_ACTION_SIGNATURE and (now - _LAST_ACTION_AT) <= _DUPLICATE_WINDOW_SECONDS:
                logger.warning(
                    "[INTERCEPT] duplicate_command_suppressed cmd=browser_agent elapsed_s=%.1f",
                    now - _LAST_ACTION_AT,
                )
                return True, None

            _LAST_ACTION_SIGNATURE = signature
            _LAST_ACTION_AT = now
            confirmation = "Fechado, chefe. Vou abrir o navegador da Kira e executar isso por la."
            logger.info("[INTERCEPT] returning_direct_voice message=%r", confirmation)
            return True, confirmation
            logger.warning("[INTERCEPT] command_failed message=%r", confirmation)
            return True, confirmation
    except Exception as exc:
        logger.warning("[INTERCEPT] pc_control_browser_route_unavailable error=%s", exc)

    intent = detect_action_intent(raw_input)

    if intent is None:
        logger.debug("[INTERCEPT] not_a_command proceeding_to_gemini input=%s", raw_input[:50])
        logger.debug("[SYSTEM_ACTION] no_action_detected input=%s", raw_input[:50])
        return False, None

    logger.info("[SYSTEM_ACTION] action_detected type=%s target=%s", intent.action_type, intent.target)

    success, message = execute_action(intent)

    if success:
        logger.info("[SYSTEM_ACTION] action_executed message=%s", message)
        return True, message

    logger.warning("[SYSTEM_ACTION] action_failed message=%s", message)
    return True, message
