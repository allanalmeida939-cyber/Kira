from __future__ import annotations

from livekit.agents import function_tool

from services.persistent_agenda import AgendaError


class AgendaMixin:
    @function_tool(description="Cria um evento na agenda local. Use para pedidos como 'marca reuniao amanha 14h' ou 'agenda dentista em 2026-03-15 09:30'.")
    async def criar_evento_agenda(
        self,
        titulo: str,
        data: str,
        hora: str,
    ) -> str:
        """Cria um evento na agenda local.

        Args:
            titulo: Titulo do evento.
            data: Data em hoje, amanha, YYYY-MM-DD ou DD/MM/YYYY.
            hora: Horario em formatos como 14h, 14:00 ou 14h30.
        """
        try:
            evento = await self._run_pc_action(
                self._agenda.create_event,
                titulo,
                data,
                hora,
            )
        except AgendaError as exc:
            return str(exc)

        return (
            f"Evento criado: {evento['titulo']}\n"
            f"Quando: {evento['data']} {evento['hora']}\n"
            f"Id: {evento['id']}"
        )

    @function_tool(description="Cria um evento na agenda a partir de uma frase natural. Use para pedidos como 'marca reuniao sexta as 15h' ou 'agenda dentista amanha 9h'.")
    async def criar_evento_agenda_natural(
        self,
        frase: str,
    ) -> str:
        """Cria um evento da agenda a partir de frase completa.

        Args:
            frase: Pedido completo do usuario.
        """
        try:
            evento = await self._run_pc_action(
                self._agenda.create_event_from_text,
                frase,
            )
        except AgendaError as exc:
            return str(exc)

        return (
            f"Evento criado: {evento['titulo']}\n"
            f"Quando: {evento['data']} {evento['hora']}\n"
            f"Id: {evento['id']}"
        )

    @function_tool(description="Consulta a agenda local. Use para pedidos como 'o que tenho hoje?', 'o que tenho amanha?' ou 'quais eventos no dia 2026-03-15?'.")
    async def consultar_agenda(
        self,
        data: str = "hoje",
    ) -> str:
        """Consulta eventos da agenda local.

        Args:
            data: Hoje, amanha, YYYY-MM-DD ou DD/MM/YYYY.
        """
        try:
            eventos = await self._run_pc_action(self._agenda.events_for_date, data)
        except AgendaError as exc:
            return str(exc)

        if not eventos:
            return f"Nao ha eventos na agenda para {data}."

        linhas = [f"Eventos em {eventos[0]['data']}:"]
        for evento in eventos:
            linhas.append(f"- {evento['hora']} | {evento['titulo']}")
        return "\n".join(linhas)

    @function_tool(description="Cancela um evento da agenda pelo id ou por parte do titulo. Use para pedidos como 'cancela a reuniao de sexta' ou 'remove o evento abc123'.")
    async def cancelar_evento_agenda(
        self,
        identificador: str,
    ) -> str:
        """Cancela um evento da agenda.

        Args:
            identificador: Id do evento ou parte do titulo.
        """
        try:
            evento = await self._run_pc_action(
                self._agenda.cancel_event,
                identificador,
            )
        except AgendaError as exc:
            return str(exc)

        return (
            f"Evento cancelado: {evento['titulo']}\n"
            f"Quando era: {evento['data']} {evento['hora']}\n"
            f"Id: {evento['id']}"
        )

    @function_tool(description="Verifica reunioes e eventos proximos para avisar quando faltam 30 minutos, 10 minutos, na hora ou quando ja houve atraso.")
    async def avisar_reunioes(
        self,
    ) -> str:
        """Checa eventos proximos da agenda."""
        try:
            avisos = await self._run_pc_action(self._agenda.check_upcoming_events)
        except AgendaError as exc:
            return str(exc)

        if not avisos:
            return "Nenhuma reuniao ou evento exigindo aviso agora."
        return "\n".join(avisos)

    @function_tool(description="Diz quanto tempo falta para um evento e alerta quando esta em cima da hora. Use para pedidos como 'quanto falta pra reuniao?' ou 'vou me atrasar para a reuniao de hoje?'.")
    async def avisar_tempo_restante(
        self,
        identificador: str,
        margem_min: int = 10,
    ) -> str:
        """Calcula tempo restante para um evento.

        Args:
            identificador: Id do evento ou parte do titulo.
            margem_min: Margem em minutos para considerar atraso iminente.
        """
        try:
            return await self._run_pc_action(
                self._agenda.time_remaining_for_event,
                identificador,
                margem_min,
            )
        except AgendaError as exc:
            return str(exc)

    @function_tool(description="Detecta eventos esquecidos ou atrasados com base no status pendente e no horario que ja passou.")
    async def detectar_atraso_ou_esquecimento(
        self,
    ) -> str:
        """Lista eventos pendentes que ja passaram do horario."""
        try:
            atrasos = await self._run_pc_action(self._agenda.detect_overdue_items)
        except AgendaError as exc:
            return str(exc)

        if not atrasos:
            return "Nao encontrei tarefas ou reunioes esquecidas agora."
        return "\n".join(atrasos)

    @function_tool(description="Atualiza o status de um evento da agenda. Use para marcar como pendente, em_andamento, feito ou atrasado.")
    async def atualizar_status_evento(
        self,
        identificador: str,
        status: str,
    ) -> str:
        """Atualiza status de um evento.

        Args:
            identificador: Id do evento ou parte do titulo.
            status: pendente, em_andamento, feito ou atrasado.
        """
        try:
            evento = await self._run_pc_action(
                self._agenda.mark_event_status,
                identificador,
                status,
            )
        except AgendaError as exc:
            return str(exc)

        return (
            f"Status atualizado: {evento['titulo']}\n"
            f"Novo status: {evento['status']}\n"
            f"Id: {evento['id']}"
        )


__all__ = ["AgendaMixin"]
