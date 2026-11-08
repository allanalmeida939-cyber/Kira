"""Tools de download de vídeo/áudio: 'baixa esse vídeo', 'só o áudio', 'baixa e manda pro Breno'."""

from __future__ import annotations

import asyncio
import logging

from livekit.agents import function_tool

try:
    from runtime import video_download as vd
    from runtime.whatsapp_media_tools import OFFLINE, _bridge
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime import video_download as vd
    from agent.runtime.whatsapp_media_tools import OFFLINE, _bridge

logger = logging.getLogger(__name__)
WHATSAPP_MAX_BYTES = 64 * 1024 * 1024
# Download curto termina dentro da própria tool: uma resposta só, em vez de "comecei" + aviso + eco do Gemini.
QUICK_WAIT_S = 6.0


class DownloadToolsMixin:
    _downloader: vd.Downloader | None = None

    def _get_downloader(self) -> vd.Downloader:
        if self._downloader is None:
            self._downloader = vd.Downloader()
        return self._downloader

    async def _download_message(self, item: vd.Download, enviar_para: str) -> str:
        """Resultado do download; se pedido, manda pelo WhatsApp."""
        if item.state != "pronto" or item.file is None:
            message = f"Não consegui baixar o vídeo: {item.error}"
        elif enviar_para:
            size = item.file.stat().st_size if item.file.exists() else 0
            if size > WHATSAPP_MAX_BYTES:
                message = f"Baixei '{item.title}', mas tem {size // (1024 * 1024)} MB e o WhatsApp aceita até 64. Quer em qualidade menor?"
            else:
                result = await _bridge("POST", "/send-media", json={"contact": enviar_para, "path": str(item.file), "caption": ""})
                sent = "e mandei" if result.get("success") else f"mas não consegui mandar ({result.get('message') or OFFLINE})"
                message = f"Baixei '{item.title}' {sent} para {enviar_para}."
        else:
            message = f"Pronto, baixei '{item.title}'. Tá na pasta Downloads, em Kira."
        return message

    async def _finish_download(self, item: vd.Download, enviar_para: str) -> None:
        """Fim de um download longo: avisa por voz."""
        message = await self._download_message(item, enviar_para)
        coordinator = getattr(self, "_speech_coordinator", None)
        if coordinator is not None:
            await coordinator.speak_proactive_message(message, label="DOWNLOAD", add_to_chat_ctx=False)
        else:
            logger.info("DOWNLOAD_NOTICE_NOT_SPOKEN reason=no_coordinator")

    @function_tool(
        name="baixar_video",
        description=(
            "Baixa vídeo ou só o áudio de YouTube, Instagram, TikTok, X e outros sites. url: o link se o Chefe disser; "
            "vazio = o link copiado ou o do navegador aberto (chame com url vazio ANTES de pedir o link). qualidade: 'melhor', '1080', '720', '480' ou 'audio' "
            "(mp3). Se ele não disse a qualidade, PERGUNTE antes de chamar. enviar_para: contato do WhatsApp se ele "
            "pedir para mandar depois. Repita o resultado: se já baixou, diga que baixou; se começou, diga só que começou (você avisa ao terminar)."
        ),
    )
    async def baixar_video(self, qualidade: str, url: str = "", enviar_para: str = "") -> str:
        quality = vd.quality_key(qualidade)
        if quality is None:
            return "Pergunte a qualidade: melhor, 1080, 720, 480 ou só o áudio."
        link = vd.find_url(url)
        source = "pedido"
        if link is None:
            link, source = vd.find_url(await asyncio.to_thread(vd.clipboard_text)), "copiado"
        if link is None:
            link, source = await asyncio.to_thread(vd.browser_url), "navegador"
        if link is None:
            return "Não achei nenhum link: peça para o Chefe copiar o link do vídeo (ou abrir o vídeo no navegador)."
        loop = asyncio.get_running_loop()
        target = str(enviar_para or "").strip()
        finished: asyncio.Future = loop.create_future()

        def done(item: vd.Download) -> None:
            loop.call_soon_threadsafe(lambda: finished.done() or finished.set_result(item))

        self._get_downloader().start(link, quality, done)
        logger.info("DOWNLOAD_STARTED source=%s quality=%s", source, quality)
        try:
            return await self._download_message(await asyncio.wait_for(asyncio.shield(finished), QUICK_WAIT_S), target)
        except asyncio.TimeoutError:
            finished.add_done_callback(lambda future: loop.create_task(self._finish_download(future.result(), target)))
        what = "o áudio" if quality == "audio" else f"o vídeo ({'melhor qualidade' if quality == 'melhor' else quality + 'p'})"
        origin = {"pedido": "", "copiado": " do link copiado", "navegador": " do navegador"}[source]
        return f"Comecei a baixar {what}{origin}. Aviso quando terminar."

    @function_tool(
        name="status_download",
        description="Diz como estão os downloads de vídeo: 'já baixou?', 'quanto falta?'.",
    )
    async def status_download(self) -> str:
        items = self._get_downloader().items
        if not items:
            return "Nenhum download agora."
        return " ".join(item.summary() for item in items[-3:])


__all__ = ["DownloadToolsMixin"]
