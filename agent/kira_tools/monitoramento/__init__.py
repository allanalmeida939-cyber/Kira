from __future__ import annotations

from livekit.agents import function_tool

from visual_watchers import VisualWatchError
from watcher import WatcherError


class MonitoramentoMixin:
    @function_tool(description="Inicia um monitoramento em background. Use para pedidos como 'me avisa quando mudar a tela', 'me avisa quando terminar de carregar', 'me avisa quando aparecer erro' ou 'me avisa quando sumir carregando'. So use quando o modo visao estiver ativo. Modos: mudanca_tela, tela_estavel, janela_contem, janela_nao_contem.")
    async def iniciar_monitoramento(
        self,
        modo: str,
        alvo: str | None = None,
        timeout_segundos: int = 120,
        intervalo_segundos: int = 1,
        estabilidade_segundos: int = 3,
    ) -> str:
        """Liga um watcher em background.

        Args:
            modo: Use mudanca_tela, tela_estavel, janela_contem ou janela_nao_contem.
            alvo: Texto monitorado no titulo da janela quando o modo exigir.
            timeout_segundos: Tempo limite do monitoramento. Use 0 ou negativo para deixar sem prazo.
            intervalo_segundos: Intervalo entre verificacoes.
            estabilidade_segundos: Tempo de tela estavel para o modo tela_estavel.
        """
        normalized_mode = (modo or "").strip().lower()
        safe_interval = max(int(intervalo_segundos), 1)
        safe_timeout = None if timeout_segundos <= 0 else max(int(timeout_segundos), 5)
        safe_stability = max(int(estabilidade_segundos), 2)

        if not self._visual_context_active:
            return "Nao estou vendo a tela agora. Ativa o compartilhamento que eu monitoro."

        try:
            if normalized_mode in {"mudanca_tela", "mudou_tela", "mudanca"}:
                watcher = self._watchers.start_screen_change_watcher(
                    interval_seconds=safe_interval,
                    timeout_seconds=safe_timeout,
                    success_message="Chefe, a tela mudou.",
                    timeout_message="Nao vi mudanca relevante na tela dentro do tempo combinado.",
                )
                return f"Watcher ligado. Vou te avisar quando a tela mudar. ID: {watcher.watcher_id}"

            if normalized_mode in {"tela_estavel", "carregou", "fim_carregamento", "terminou"}:
                watcher = self._watchers.start_screen_stable_watcher(
                    interval_seconds=safe_interval,
                    timeout_seconds=safe_timeout,
                    stable_seconds=safe_stability,
                    success_message="Chefe, a tela estabilizou. Isso ta com cara de carregamento concluido.",
                    timeout_message="Nao vi a tela estabilizar dentro do tempo combinado.",
                )
                return f"Watcher ligado. Vou te avisar quando a tela estabilizar. ID: {watcher.watcher_id}"

            if normalized_mode in {"janela_contem", "texto_apareceu", "apareceu"}:
                watcher = self._watchers.start_window_title_contains_watcher(
                    alvo or "",
                    interval_seconds=safe_interval,
                    timeout_seconds=safe_timeout,
                    success_message=f"Chefe, apareceu '{(alvo or '').strip()}' no titulo da janela.",
                    timeout_message=f"Nao vi '{(alvo or '').strip()}' aparecer no titulo da janela a tempo.",
                )
                return (
                    "Watcher ligado. Vou te avisar quando esse texto aparecer no titulo da janela. "
                    f"ID: {watcher.watcher_id}"
                )

            if normalized_mode in {"janela_nao_contem", "texto_sumiu", "sumiu"}:
                watcher = self._watchers.start_window_title_not_contains_watcher(
                    alvo or "",
                    interval_seconds=safe_interval,
                    timeout_seconds=safe_timeout,
                    success_message=f"Chefe, '{(alvo or '').strip()}' sumiu do titulo da janela.",
                    timeout_message=f"'{(alvo or '').strip()}' ainda nao sumiu do titulo da janela.",
                )
                return (
                    "Watcher ligado. Vou te avisar quando esse texto sumir do titulo da janela. "
                    f"ID: {watcher.watcher_id}"
                )
        except WatcherError as exc:
            return str(exc)

        return "Modo de monitoramento invalido. Use mudanca_tela, tela_estavel, janela_contem ou janela_nao_contem."

    @function_tool(description="Inicia um monitoramento visual sob demanda em background. Use quando o usuario disser 'me avisa quando terminar', 'quando carregar', 'quando abrir', 'quando aparecer', 'quando sumir', 'fica de olho nisso' ou pedidos parecidos. So funciona com modo visao ativo.")
    async def iniciar_monitoramento_visual(
        self,
        pedido: str,
        timeout_segundos: int = 0,
    ) -> str:
        """Liga um watcher visual sob demanda usando a tela atual atual.

        Args:
            pedido: Pedido natural do usuario, como 'me avisa quando terminar de baixar'.
            timeout_segundos: Tempo limite. Use 0 para manter o timeout padrao do watcher.
        """
        if not self._visual_context_active:
            return "Nao estou vendo a tela agora. Ativa o compartilhamento que eu monitoro."

        safe_timeout = None if timeout_segundos <= 0 else max(int(timeout_segundos), 15)
        try:
            result = await self._visual_watchers.start_from_request(
                pedido,
                timeout_seconds=safe_timeout,
            )
        except VisualWatchError as exc:
            return str(exc)

        suffix = " (ja existia)." if result.duplicate else ""
        return f"{result.confirmation_message} ID: {result.watcher.watcher_id}{suffix}"

    @function_tool(description="Lista os monitoramentos em background ativos e seus ids.")
    async def listar_monitoramentos(self) -> str:
        """Mostra os watchers ativos."""
        legacy_watchers = self._watchers.list_watchers()
        visual_watchers = self._visual_watchers.list_watchers()
        if not legacy_watchers and not visual_watchers:
            return "Nao tem monitoramento ativo agora."

        lines = ["Monitoramentos ativos:"]
        for watcher in visual_watchers:
            lines.append(
                f"- {watcher.watcher_id}: visual {watcher.kind} ha {watcher.age_seconds}s. {watcher.description}"
            )
        for watcher in legacy_watchers:
            lines.append(
                f"- {watcher.watcher_id}: {watcher.mode} ha {watcher.age_seconds}s. {watcher.description}"
            )
        return "\n".join(lines)

    @function_tool(description="Para um monitoramento em background pelo id ou encerra todos de uma vez. Use para pedidos como 'nao precisa mais', 'pode parar de observar', 'cancela esse monitoramento'.")
    async def parar_monitoramento(
        self,
        monitor_id: str | None = None,
        todos: bool = False,
    ) -> str:
        """Para watcher(s) ativos.

        Args:
            monitor_id: Id de um monitoramento especifico.
            todos: Se verdadeiro, encerra todos os watchers.
        """
        if todos:
            total = self._watchers.cancel_all() + self._visual_watchers.cancel_all()
            if total == 0:
                return "Nao tinha monitoramento ativo para parar."
            return f"Parei {total} monitoramento(s)."

        active_watchers = [*self._visual_watchers.list_watchers(), *self._watchers.list_watchers()]
        target_id = (monitor_id or "").strip()

        if not target_id and len(active_watchers) == 1:
            target_id = active_watchers[0].watcher_id

        if not target_id:
            return "Me diz o id do monitoramento que eu devo parar, ou manda parar todos."

        if self._visual_watchers.cancel_watcher(target_id) or self._watchers.cancel_watcher(target_id):
            return f"Monitoramento {target_id} parado."

        return f"Nao encontrei monitoramento ativo com id {target_id}."


__all__ = ["MonitoramentoMixin"]
