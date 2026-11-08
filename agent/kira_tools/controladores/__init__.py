from __future__ import annotations

import logging

from livekit.agents import function_tool

from pc_control import PCControlError, parse_duration_to_seconds
from ui_session_intent_resolver import (
    DOMAIN_SESSION_CONTROL,
    DOMAIN_SYSTEM_CONTROL,
    DOMAIN_UI_CONTROL,
    parse_ui_session_intent,
)
from volume_intent_resolver import parse_volume_intent

if __package__ and __package__.startswith("agent"):
    from agent.core.command_intent import classify_explicit_command
else:  # pragma: no cover - suporte a execucao direta em agent/
    from core.command_intent import classify_explicit_command

logger = logging.getLogger(__name__)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _is_explicit_system_audio_request(text: str) -> bool:
    return _contains_any(text, ("pc", "computador", "windows", "sistema"))


def _looks_like_kira_session_command(text: str) -> bool:
    return _contains_any(
        text,
        (
            "microfone",
            "microfono",
            "mic",
            "camera",
            "webcam",
            "tela",
            "visao",
            "compartilhamento",
            "modo visao",
            "aba",
            "config",
            "whatsapp",
            "painel",
            "kira",
            "sessao",
        ),
    )


def _require_explicit_command(instance, *, action_label: str, fallback_text: str = "") -> str | None:
    guard = getattr(instance, "ensure_explicit_user_command", None)
    if callable(guard):
        return guard(action_label=action_label, fallback_text=fallback_text)
    decision = classify_explicit_command(fallback_text)
    logger.info(
        "[INTENT] text=%r is_command=%s confidence=%.2f action=%s reason=%s",
        fallback_text[:120],
        decision.is_command,
        decision.confidence,
        action_label,
        decision.reason,
    )
    if decision.is_command:
        return None
    return (
        f"Entendi isso mais como conversa do que como comando para {action_label}. "
        "Se quiser que eu aja, me pede de forma direta."
    )


class ControladoresMixin:
    @function_tool(
        description=(
            "Controla o volume DO SISTEMA (audio do PC, nao o microfone da Kira). "
            "Suporta volume absoluto (ex: 'coloca em 30%', 'deixa em 50') e relativo "
            "(ex: 'aumenta', 'baixa um pouco'), alem de mute/unmute. "
            "NAO use esta ferramenta para controlar o microfone da Kira - use controlar_sessao_kira. "
            "Volume DA MUSICA/DO VIDEO do player da Kira ('abaixa o volume da musica') e control_music, nao esta."
        )
    )
    async def controlar_volume(self, acao: str, percentual: int | None = None) -> str:
        """Controla o volume do sistema.

        Args:
            acao: Frase natural de volume (absoluto/relativo/mute/unmute) ou acao direta.
            percentual: Valor de 0 a 100 quando houver pedido absoluto direto.
        """
        try:
            acao_norm = (acao or "").strip().lower()
            blocked = _require_explicit_command(self, action_label="ajustar o volume do PC", fallback_text=acao_norm)
            if blocked is not None:
                return blocked
            if percentual is None and _looks_like_kira_session_command(acao_norm) and not _is_explicit_system_audio_request(acao_norm):
                logger.info(
                    "system_audio_action_blocked_due_to_ui_priority command=%s",
                    acao,
                )
                return (
                    "Esse pedido parece ser da interface/sessao da Kira, nao do volume global do Windows. "
                    "Use o controle de interface da Kira para mic/camera/tela/abas."
                )

            if percentual is not None:
                logger.info("volume_intent_parsed intent=set_volume_absolute requested_value=%s", percentual)
                return await self._run_pc_action(self._pc.set_volume_absolute, percentual)

            parsed = parse_volume_intent(acao)
            logger.info("volume_intent_parsed intent=%s value=%s direction=%s step=%s raw=%s", parsed.intent, parsed.value, parsed.direction, parsed.step, acao)
            if parsed.intent == "set_volume_absolute" and parsed.value is not None:
                return await self._run_pc_action(self._pc.set_volume_absolute, parsed.value)
            if parsed.intent == "adjust_volume_relative" and parsed.direction:
                return await self._run_pc_action(
                    self._pc.adjust_volume_relative,
                    parsed.direction,
                    parsed.step or "default",
                )
            if parsed.intent == "mute_volume":
                return await self._run_pc_action(self._pc.mute_volume)
            if parsed.intent == "unmute_volume":
                return await self._run_pc_action(self._pc.unmute_volume)

            normalized = acao.strip().lower()
            aliases = {
                "aumentar volume": "aumentar",
                "diminuir volume": "diminuir",
                "volume maximo": "maximo",
                "maximo": "maximo",
                "silenciar": "silenciar",
                "mute": "silenciar",
                "desmutar": "desmutar",
                "dessilenciar": "dessilenciar",
            }
            normalized = aliases.get(normalized, normalized)
            return await self._run_pc_action(self._pc.control_volume, normalized)
        except PCControlError as exc:
            return str(exc)

    @function_tool(
        description=(
            "Controla a INTERFACE/SESSAO da Kira por comando natural. "
            "Use para microfone, camera, tela/visao/compartilhamento e abas internas (Kira, WhatsApp, Config). "
            "Prioriza interface da Kira; so use controle de sistema quando o usuario disser explicitamente PC/Windows/sistema."
        )
    )
    async def controlar_interface_kira(self, comando: str) -> str:
        blocked = _require_explicit_command(self, action_label="controlar a interface da Kira", fallback_text=comando)
        if blocked is not None:
            return blocked
        intent = parse_ui_session_intent(comando)
        logger.info(
            "ui_action_classified domain=%s intent=%s target=%s desired_state=%s command=%s",
            intent.domain,
            intent.intent,
            intent.target,
            intent.desired_state,
            comando,
        )

        if intent.domain == DOMAIN_UI_CONTROL and intent.intent == "open_tab" and intent.target:
            publish_fn = getattr(self, "_publish_ui_control", None)
            if not callable(publish_fn):
                return "[KIRA UI] ui_action_failed action=open_tab reason=bridge_unavailable"
            try:
                result = await publish_fn("tab", True, target=intent.target, action="open_tab")
                if result.get("ok"):
                    return f"[KIRA UI] ui_action_success action=open_tab active_tab={result.get('state_after', {}).get('active_tab', intent.target)}"
                return f"[KIRA UI] ui_action_failed action=open_tab reason={result.get('error', 'unknown_error')}"
            except Exception as exc:
                return f"[KIRA UI] ui_action_failed action=open_tab reason={exc}"

        if intent.domain == DOMAIN_SESSION_CONTROL and intent.target:
            desired = intent.desired_state if intent.desired_state is not None else False
            estado = "ligado" if desired else "desligado"
            publish_fn = getattr(self, "_publish_ui_control", None)
            if not callable(publish_fn):
                return f"[KIRA UI] ui_action_failed action=session_toggle recurso={intent.target} reason=bridge_unavailable"
            try:
                result = await publish_fn(intent.target, bool(desired), action="set_session_control")
                if result.get("ok"):
                    return f"[KIRA UI] ui_action_success action=session_toggle recurso={intent.target} estado={estado}"
                return f"[KIRA UI] ui_action_failed action=session_toggle recurso={intent.target} reason={result.get('error', 'unknown_error')}"
            except Exception as exc:
                return f"[KIRA UI] ui_action_failed action=session_toggle recurso={intent.target} reason={exc}"

        if intent.domain == DOMAIN_SYSTEM_CONTROL:
            return await self.controlar_volume(comando)

        return "Nao consegui classificar esse comando de interface/sessao com seguranca."

    @function_tool(
        description=(
            "Consulta o estado real da interface/sessao da Kira: aba ativa, mic, camera e tela."
        )
    )
    async def consultar_estado_interface_kira(self) -> str:
        state_fn = getattr(self, "_get_kira_ui_state", None)
        if not callable(state_fn):
            return "[KIRA UI] ui_state_unavailable reason=bridge_unavailable"
        try:
            state = await state_fn()
        except Exception as exc:
            return f"[KIRA UI] ui_state_failed motivo={exc}"
        return (
            "[KIRA UI] ui_state "
            f"active_tab={state.get('active_tab')} "
            f"mic_enabled={state.get('mic_enabled')} "
            f"camera_enabled={state.get('camera_enabled')} "
            f"screen_enabled={state.get('screen_enabled')} "
            f"session_connected={state.get('session_connected')}"
        )

    @function_tool(
        description=(
            "Define volume absoluto do sistema em porcentagem (0-100). "
            "Use quando o usuario pedir valor exato."
        )
    )
    async def definir_volume_absoluto(self, percentual: int) -> str:
        try:
            return await self._run_pc_action(self._pc.set_volume_absolute, percentual)
        except PCControlError as exc:
            return str(exc)

    @function_tool(description="Controla energia e tela do PC. Use para bloquear, desligar, reiniciar ou suspender. Para desligar, reiniciar e suspender, so use depois de confirmar com o usuario.")
    async def controlar_energia(
        self,
        acao: str,
        confirmado: bool = False,
    ) -> str:
        """Controla energia e bloqueio.

        Args:
            acao: Use bloquear, desligar, reiniciar ou suspender.
            confirmado: True somente com confirmacao explicita do usuario para acoes destrutivas.
        """
        blocked = _require_explicit_command(self, action_label="controlar a energia do PC", fallback_text=acao)
        if blocked is not None:
            return blocked
        normalized = acao.lower()
        if normalized in {"desligar", "reiniciar", "suspender"} and not confirmado:
            return "Preciso de confirmacao explicita antes dessa acao de energia."

        try:
            return await self._run_pc_action(self._pc.control_power, normalized)
        except PCControlError as exc:
            return str(exc)

    # FASE 0 — tools de browser do Gemini (abrir_na_web / abrir_app_ou_link) REMOVIDOS.
    # O navegador agora tem caminho unico (fluxo por contexto no assistant + Browser
    # Agent externo p/ embutido, open_in_local_browser p/ navegador local). O Gemini
    # nao abre mais navegador por tool (eliminada a corrida que abria abas de lixo).

    @function_tool(description="Abre ou fecha programas do Windows. Para comandos genericos como 'fecha isso' ou 'fecha a pasta', feche a janela em foco com seguranca. Se houver modo visao ativo, use o contexto visual atual so como pista para decidir o alvo. Nunca use fechar para matar explorer.exe; so use encerrar, matar ou reiniciar o Explorer quando o pedido for explicitamente destrutivo.")
    async def controlar_programa(
        self,
        acao: str,
        programa: str,
    ) -> str:
        """Abre ou fecha programas.

        Args:
            acao: Use abrir, fechar (fecha a janela, como o X), encerrar ou matar (termina o processo à força) ou reiniciar.
            programa: Nome do programa, por exemplo discord, vscode, chrome.
        """
        try:
            blocked = _require_explicit_command(
                self,
                action_label="controlar um programa",
                fallback_text=f"{acao} {programa}".strip(),
            )
            if blocked is not None:
                return blocked
            return await self._run_pc_action(self._pc.control_program, acao.lower(), programa)
        except PCControlError as exc:
            return str(exc)

    @function_tool(description="Ferramentas de tempo: diz as horas, inicia cronometro ou inicia temporizador.")
    async def utilidade_tempo(
        self,
        acao: str,
        duracao: str | None = None,
    ) -> str:
        """Executa utilidades de tempo.

        Args:
            acao: Use horas, cronometro, status_cronometro ou temporizador.
            duracao: Duracao do temporizador, por exemplo 10, 30 segundos, 5 minutos, 1h 20m.
        """
        try:
            normalized = acao.lower()
            if normalized in {"horas", "que horas sao", "hora"}:
                return await self._run_pc_action(self._pc.current_time)
            if normalized == "cronometro":
                return await self._run_pc_action(self._pc.start_stopwatch)
            if normalized == "status_cronometro":
                return await self._run_pc_action(self._pc.stopwatch_status)
            if normalized == "temporizador":
                if not duracao:
                    return "Informe a duracao do temporizador."
                seconds = parse_duration_to_seconds(duracao)
                return await self._run_pc_action(self._pc.start_timer, seconds)
            raise PCControlError("Acao de tempo invalida.")
        except PCControlError as exc:
            return str(exc)

    @function_tool(
        description=(
            "Tira screenshot da tela. Por padrao salva na pasta da Kira (Documents/Kira/Screenshots) "
            "e copia para o clipboard. Se o usuario pedir caminho ou nome especifico, use caminho_salvar. "
            "NAO salva no Desktop a menos que o usuario peca explicitamente."
        )
    )
    async def tirar_print(
        self,
        caminho_salvar: str | None = None,
        copiar_clipboard: bool = True,
    ) -> str:
        """Captura a tela.

        Args:
            caminho_salvar: Caminho completo onde salvar. Deixe None para usar pasta padrao da Kira.
            copiar_clipboard: Se True (padrao), copia a imagem para o clipboard apos salvar.
        """
        try:
            return await self._run_pc_action(
                self._pc.take_screenshot,
                caminho_salvar,
                copiar_clipboard,
            )
        except PCControlError as exc:
            return str(exc)

    @function_tool(description="Utilidades simples do sistema. Use tirar_print para screenshots.")
    async def utilidade_sistema(self, acao: str) -> str:
        """Executa utilidades do sistema.

        Args:
            acao: Use print (legado â€” prefira tirar_print diretamente).
        """
        try:
            if acao.lower() not in {"print", "tirar print", "screenshot"}:
                raise PCControlError("Acao de sistema invalida.")
            return await self._run_pc_action(self._pc.take_screenshot)
        except PCControlError as exc:
            return str(exc)


    @function_tool(
        description=(
            "Controla elementos DA PROPRIA INTERFACE DA KIRA: microfone da sessao, "
            "modo visao, webcam e painel de transcricao. "
            "USE ESTE TOOL quando o usuario pedir para ligar/desligar o microfone DA KIRA, "
            "modo visao, camera ou transcricao. "
            "NAO confunda com controlar_volume (volume do sistema) nem com controlar_energia. "
            "Exemplos validos: 'desliga meu mic', 'para o modo visao', 'abre transcricao', 'desliga camera'."
        )
    )
    async def controlar_sessao_kira(self, recurso: str, acao: str) -> str:
        """Controla recursos internos da sessao da Kira.

        Args:
            recurso: mic, vision_mode, webcam ou transcricao.
            acao: ligar ou desligar.
        """
        recurso_norm = recurso.strip().lower()
        acao_norm = acao.strip().lower()

        ligar = acao_norm in {"ligar", "ativar", "abrir", "iniciar", "on", "true"}
        desligar = acao_norm in {"desligar", "desativar", "fechar", "parar", "off", "false"}

        if not ligar and not desligar:
            return f"Acao '{acao}' invalida. Use ligar ou desligar."

        estado = "ligado" if ligar else "desligado"

        # Publica o comando para a interface via data channel
        # A interface ouve o evento kira_ui_control e aplica o toggle correto
        recurso_map = {
            "mic": "mic",
            "microfone": "mic",
            "microphone": "mic",
            "screen": "vision_mode",
            "vision_mode": "vision_mode",
            "tela": "vision_mode",
            "compartilhamento": "vision_mode",
            "webcam": "webcam",
            "camera": "webcam",
            "cam": "webcam",
            "transcricao": "transcription",
            "transcription": "transcription",
            "legenda": "transcription",
        }
        recurso_key = recurso_map.get(recurso_norm)
        if not recurso_key:
            return (
                f"Recurso '{recurso}' nao reconhecido. "
                "Use: mic, vision_mode, webcam ou transcricao."
            )

        publish_fn = getattr(self, "_publish_ui_control", None)
        if callable(publish_fn):
            try:
                result = await publish_fn(recurso_key, ligar, action="set_session_control")
                if result.get("ok"):
                    return f"[KIRA UI] kira_ui_control_applied recurso={recurso_key} estado={estado}"
                return f"[KIRA UI] kira_ui_control_failed recurso={recurso_key} motivo={result.get('error', 'unknown_error')}"
            except Exception as exc:
                return f"[KIRA UI] kira_ui_control_failed recurso={recurso_key} motivo={exc}"

        # Fallback: sem bridge disponÃ­vel, registra a intenÃ§Ã£o
        return (
            f"Comando recebido: {recurso_key} -> {estado}. "
            "Interface sera sincronizada na proxima oportunidade."
        )


__all__ = ["ControladoresMixin"]
