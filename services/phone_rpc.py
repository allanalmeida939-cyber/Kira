"""Chamada de ida e volta com a tela do celular (Olho de Deus e outros recursos que respondem algo).

O backend publica {type: 'ui_call', windowId: 'celular', id, target, method, args} na conexão dos painéis; a tela do
celular executa só alvos e métodos conhecidos (interface/assets/js/ui/core/dispatcher.js) e devolve o resultado em
POST /api/phone/ui-result. Sem resposta no prazo: None (quem chamou diz que o recurso não respondeu).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any, Awaitable, Callable

logger = logging.getLogger("kira.app.remote")

TARGETS = {"godseye": frozenset({"command", "showPlace"}), "rota": frozenset({"show"})}
MAX_ARGS = 20000


class PhoneRpc:
    def __init__(self, publish: Callable[[list[dict[str, Any]]], Awaitable[int]]) -> None:
        self._publish = publish
        self._pending: dict[str, asyncio.Future] = {}

    async def call(self, target: str, method: str, args: Any, timeout_s: float = 10.0) -> Any:
        if method not in TARGETS.get(target, ()) or len(json.dumps(args, ensure_ascii=False)) > MAX_ARGS:
            raise ValueError("chamada desconhecida")
        call_id = secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        try:
            delivered = await self._publish([{"type": "ui_call", "windowId": "celular", "id": call_id, "target": target,
                                               "method": method, "args": args}])
            if not delivered:
                return None
            return await asyncio.wait_for(future, timeout_s)
        except asyncio.TimeoutError:
            logger.info("PHONE_UI_CALL_TIMEOUT target=%s method=%s", target, method)
            return None
        finally:
            self._pending.pop(call_id, None)

    def resolve(self, call_id: str, result: Any) -> bool:
        future = self._pending.get(str(call_id or ""))
        if future is None or future.done():
            return False
        future.set_result(result)
        return True


__all__ = ["PhoneRpc", "TARGETS"]
