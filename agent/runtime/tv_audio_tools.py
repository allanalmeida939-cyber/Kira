"""Voz da Kira saindo na TV (2026-11-06): a página da TV entra na sala de voz só para ouvir e o PC fica mudo."""

from __future__ import annotations

import json

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local


class TvAudioToolsMixin:
    @function_tool(
        name="voz_na_tv",
        description=(
            "Liga/desliga a VOZ da Kira saindo pela TV (Wi-Fi) em vez do fone/caixa do PC. ligar=true para 'fala pela TV', "
            "'começa a falar na TV', 'som na TV', 'quero te ouvir na TV'; ligar=false para 'volta pro fone', 'fala pelo PC', "
            "'tira o som da TV'. Abre a tela da Kira na TV se precisar. Na 1ª vez a TV pode pedir 'aperte OK no controle'. "
            "Só confirme com ok=true e confirmed=true."
        ),
    )
    async def voz_na_tv(self, ligar: bool = True, aparelho: str = "tv") -> str:
        try:
            r = await _post_local("/api/tv/audio", {"ligar": bool(ligar), "aparelho": aparelho}, read_timeout=40.0)
        except (httpx.HTTPError, TimeoutError, ValueError):
            return "Não consegui falar com a TV agora."
        return json.dumps(r, ensure_ascii=False)
