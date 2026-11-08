from __future__ import annotations

from livekit.agents import function_tool


class WhatsAppMixin:
    @function_tool(description="Inicia o monitor do WhatsApp em background via Controller HTTP. Use quando o usuario quiser que voce acompanhe mensagens novas sem reler mensagens antigas.")
    async def iniciar_monitor_whatsapp(self) -> str:
        result = await self.start_whatsapp_monitor()
        return result.message

    @function_tool(description="Para o monitor do WhatsApp em background.")
    async def parar_monitor_whatsapp(self) -> str:
        await self.shutdown_whatsapp()
        return "Monitor do WhatsApp parado."

    @function_tool(description="Mostra o status atual do monitor do WhatsApp.")
    async def status_whatsapp(self) -> str:
        return self.describe_whatsapp_status()

    @function_tool(description="Resume as mensagens novas do WhatsApp que chegaram depois que o monitor iniciou.")
    async def resumir_mensagens_novas_whatsapp(self) -> str:
        return self.summarize_new_whatsapp_messages()

    @function_tool(
        description=(
            "Envia uma mensagem de WhatsApp para um contato. Por padrao mostra a previa "
            "e pede confirmacao explicita antes de mandar. Use quando o usuario pedir para "
            "avisar, responder ou mandar mensagem pelo WhatsApp.\n"
            "- skip_confirmation=True: SO quando o usuario disser EXPLICITAMENTE que nao "
            "precisa confirmar (ex.: 'pode mandar sem confirmar', 'nao precisa confirmar') "
            "-> manda na hora, sem perguntar 'posso mandar?'.\n"
            "- show_preview=False: SO quando o usuario disser que nao precisa mostrar/ler a "
            "previa (ex.: 'manda direto', 'nao precisa me mostrar'). "
            "Se ele disser 'manda direto sem confirmar nem mostrar', use os DOIS "
            "(skip_confirmation=True, show_preview=False).\n"
            "- message: a Kira escrevendo DIRETO para o contato, do jeito dela (cumprimente pelo "
            "nome; o Chefe sempre na 3a pessoa, nunca fale com o Chefe na mensagem), sem 'Kira:' — a "
            "assinatura e automatica.\n"
            "- como_se_fosse_o_usuario=True: SOMENTE quando o usuario pedir a mensagem 'como se "
            "fosse EU', 'como se fosse o Chefe', 'em meu nome', 'como se eu tivesse escrito' "
            "(impersona o proprio usuario). Nesse caso a mensagem sai SEM o prefixo 'Kira:'. "
            "'como se fosse VOCE' / 'do seu jeito' e o padrao (a Kira falando): NAO use o parametro."
        )
    )
    async def enviar_whatsapp(
        self,
        contact: str,
        message: str,
        skip_confirmation: bool = False,
        show_preview: bool = True,
        como_se_fosse_o_usuario: bool = False,
    ) -> str:
        """
        Envia (ou propoe) uma mensagem pelo WhatsApp para um contato.

        Toda a logica (resolucao de contato, perspectiva/assinatura "Kira:", previa,
        confirmacao e envio) vive no WhatsappOrchestrator (agent/core/whatsapp). Aqui a
        tool so encaminha. Por padrao exige confirmacao explicita antes de mandar.

        Args:
            contact: Nome do contato exatamente como salvo no WhatsApp.
            message: Conteudo da mensagem a ser enviada.
            skip_confirmation: True -> envia direto, sem pedir confirmacao.
            show_preview: False -> nao le/mostra a previa.

        Returns:
            Confirmacao, previa ou mensagem de erro.
        """
        orchestrator = getattr(self, "_whatsapp_orchestrator", None)
        if orchestrator is None:
            return "Nao consigo enviar WhatsApp neste modo."
        return await orchestrator.create_job(
            contact=contact,
            message=message,
            confirmation_mode="skip" if skip_confirmation else "required",
            preview_mode="show" if show_preview else "skip",
            first_person=bool(como_se_fosse_o_usuario),
        )


__all__ = ["WhatsAppMixin"]
