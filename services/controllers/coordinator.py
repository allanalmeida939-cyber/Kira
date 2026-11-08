"""Liga turno de conversa (turn_id do agente), pedido (requestId) e apresentação (presentationId).

Regras: um turno novo invalida os pedidos dos turnos anteriores; interrupção da fala congela a apresentação do
turno (nenhuma atualização de narração depois disso); eventos de pedidos substituídos são descartados.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field

MAX_TRACKED = 32
# Pela voz o Gemini chama a tool antes de a transcrição chegar; o turno da própria fala só é registrado depois.
# Pedido feito há menos que isto passa a ser do turno novo (antes, o painel congelava em "Pesquisando").
ADOPT_SECONDS = 12.0


@dataclass
class _Link:
    presentation_id: str
    turn_id: str | None
    frozen: bool = False
    created_at: float = field(default_factory=time.monotonic)


class ConversationPresentationCoordinator:
    def __init__(self) -> None:
        self._links: OrderedDict[str, _Link] = OrderedDict()
        self._turn_id: str | None = None
        self._latest_by_presentation: dict[str, str] = {}

    @property
    def turn_id(self) -> str | None:
        return self._turn_id

    def begin_turn(self, turn_id: str) -> None:
        now = time.monotonic()
        for link in self._links.values():
            if not link.frozen and link.turn_id != turn_id and now - link.created_at < ADOPT_SECONDS:
                link.turn_id = turn_id
        self._turn_id = turn_id

    def attach(self, request_id: str, presentation_id: str, turn_id: str | None) -> None:
        self._links[request_id] = _Link(presentation_id, turn_id or self._turn_id)
        self._latest_by_presentation[presentation_id] = request_id
        while len(self._links) > MAX_TRACKED:
            self._links.popitem(last=False)

    def accepts(self, request_id: str) -> bool:
        """Só o pedido mais novo de cada apresentação, e só se o turno dele ainda é o atual (ou não tem turno)."""
        link = self._links.get(request_id)
        if link is None or self._latest_by_presentation.get(link.presentation_id) != request_id:
            return False
        return link.turn_id is None or self._turn_id is None or link.turn_id == self._turn_id

    def interrupted(self, turn_id: str | None) -> list[str]:
        """Congela as apresentações do turno interrompido e devolve os pedidos afetados."""
        affected = [rid for rid, link in self._links.items() if turn_id is None or link.turn_id == turn_id]
        for request_id in affected:
            self._links[request_id].frozen = True
        return affected

    def reopen(self, request_id: str) -> bool:
        """Pedido explícito do usuário ("próximo", "continua"): a apresentação volta a valer, agora no turno atual."""
        link = self._links.get(request_id)
        if link is None or self._latest_by_presentation.get(link.presentation_id) != request_id:
            return False
        link.frozen = False
        link.turn_id = self._turn_id
        return True

    def can_narrate(self, request_id: str) -> bool:
        link = self._links.get(request_id)
        return link is not None and not link.frozen and self.accepts(request_id)


__all__ = ["ConversationPresentationCoordinator"]
