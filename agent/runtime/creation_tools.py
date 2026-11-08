"""Tools de criação de imagem: 'cria uma imagem de...', 'manda essa imagem pro Breno'."""

from __future__ import annotations

import asyncio
import logging

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local

logger = logging.getLogger(__name__)
UNAVAILABLE = "O criador de imagens não respondeu agora."


class CreationToolsMixin:
    _image_task: asyncio.Task | None = None

    async def _create_image_in_background(self, payload: dict) -> None:
        """A geração pode esperar o limite grátis (~50 s); segurar a tool esse tempo derrubava o turno de voz."""
        try:
            result = await _post_local("/api/ui/creation/image", payload, read_timeout=150.0)
            message = str(result.get("summary") or UNAVAILABLE)
        except (httpx.HTTPError, TimeoutError):
            message = UNAVAILABLE
        coordinator = getattr(self, "_speech_coordinator", None)
        if coordinator is not None:
            await coordinator.speak_proactive_message(message, label="CRIACAO", add_to_chat_ctx=False)
        else:
            logger.info("CREATION_NOTICE_NOT_SPOKEN reason=no_coordinator")

    @function_tool(
        name="criar_imagem",
        description=(
            "Cria uma imagem do zero a partir de uma descrição e mostra no painel Criação (salva em Imagens\\Kira). "
            "prompt: descrição detalhada EM INGLÊS (assunto, estilo, luz, cores, enquadramento). titulo: nome curto em "
            "português. formato: 'quadrado', 'paisagem' ou 'retrato'. Roda em segundo plano (até 1 minuto): diga só "
            "que começou; você avisa quando ficar pronta. Não é para buscar foto real de algo (isso é show_images). "
            "A imagem criada abre sozinha no painel Criação: nunca use show_images/recall_image para mostrá-la."
        ),
    )
    async def criar_imagem(self, prompt: str, titulo: str, formato: str = "quadrado") -> str:
        if self._image_task is not None and not self._image_task.done():
            return "Ainda estou terminando a imagem anterior; peça de novo quando ela aparecer."
        payload = {"prompt": prompt, "title": titulo, "shape": formato}
        self._image_task = asyncio.create_task(self._create_image_in_background(payload))
        return f"Comecei a criar '{titulo}'. Quando ficar pronta ela abre no painel Criação e eu aviso."

    @function_tool(
        name="enviar_imagem_criada",
        description="Manda pelo WhatsApp a última imagem criada ('manda essa imagem pro Breno').",
    )
    async def enviar_imagem_criada(self, contato: str) -> str:
        try:
            result = await _post_local("/api/ui/creation/send", {"contact": contato}, read_timeout=95.0)
        except (httpx.HTTPError, TimeoutError):
            return UNAVAILABLE
        if result.get("status") == 404:
            return "Ainda não criei nenhuma imagem nesta sessão."
        return str(result.get("summary") or "Não consegui mandar.")


__all__ = ["CreationToolsMixin"]
