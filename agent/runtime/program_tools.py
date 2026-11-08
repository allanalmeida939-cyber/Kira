"""Tool 'programa' (services/pc_operator_api.py): controlar programas do Windows sem atrapalhar o Chefe."""

from __future__ import annotations

import json

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local

UNAVAILABLE = "Não consegui mexer nesse programa agora."


class ProgramToolsMixin:
    @function_tool(
        name="programa",
        description=(
            "Controla programas do Windows. REGRA: faça sem mexer no mouse/teclado do Chefe sempre que der; mouse e teclado de "
            "verdade só se ele liberou. Liberação: quando ele disser algo como 'tô longe do PC', 'pode usar mouse e teclado', "
            "'pode mexer no PC', 'fica à vontade no computador' chame acao 'liberar' (minutos, padrão 30); quando disser "
            "'voltei', 'não mexe no mouse', 'para de mexer' chame 'bloquear'. Se a resposta vier com precisa_liberar, PERGUNTE "
            "antes ('posso usar o mouse e o teclado?'). Ao usar mouse/teclado (usou_mouse/usou_teclado), avise. acao: "
            "'midia' (tecla = play, pausa, proxima, anterior, volume_mais, volume_menos, mudo: 'no Spotify pula a música' = "
            "midia proxima; não precisa liberar); 'calcular' (texto = '35*12': abre a Calculadora e faz a conta por dentro); "
            "'abrir' (texto = programa ou arquivo); 'janelas' (o que está aberto); 'controles' (botões e campos de uma janela); "
            "'apertar' (janela + controle: aperta um botão por dentro); 'escrever' (janela + texto: escreve num campo por "
            "dentro); 'teclas' (janela + texto como 'ctrl+s': usa teclado, só liberado); 'clicar' (x, y: usa mouse, só "
            "liberado); 'status'. Para 'escreve no Bloco de Notas e salva' prefira a tool documento (criar texto) e abrir o "
            "arquivo: fica salvo sem mexer em nada."
        ),
    )
    async def programa(self, acao: str, janela: str = "", controle: str = "", texto: str = "", tecla: str = "", minutos: float = 30.0,
                       x: int = 0, y: int = 0) -> str:
        payload = {"action": acao, "window": janela, "control": controle, "text": texto, "key": tecla, "minutes": minutos, "x": x, "y": y}
        try:
            result = await _post_local("/api/ui/programa", payload, read_timeout=40.0)
        except (httpx.HTTPError, TimeoutError, ValueError):
            return UNAVAILABLE
        return json.dumps(result, ensure_ascii=False)


__all__ = ["ProgramToolsMixin"]
