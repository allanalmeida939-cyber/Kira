"""Navegador da Kira (aba Internet) pela voz: tarefas de várias etapas e o acompanhamento delas.

Abrir site e comandos com a aba aberta ("desce", "clica no segundo", "volta") ficam no atalho de voz
(try_handle_browser_task_command). Aqui: começar uma tarefa ("preenche o formulário de contato do site X"), responder
ao que o navegador pergunta e parar. A tarefa roda no backend; o laço `browser_watch_loop` acompanha o estado real da
aba (aberta ou não, esperando resposta ou não) e transforma pergunta, fim e erro da tarefa em fala da Kira.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from collections.abc import Awaitable, Callable

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _local_backend_url, _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _local_backend_url, _post_local
# Mesma ordem do resto do agente (agent.core primeiro): com "core" primeiro aqui, o estado da aba e da pergunta
# ficava num módulo e o roteador de fala lia outro ("core" e "agent.core" viram módulos diferentes), então a resposta
# falada do Chefe nunca ia para o navegador (2026-10-25).
try:
    from agent.core import browser_task_state as bts, browser_turn_context as btc
except ImportError:  # pragma: no cover - execução de dentro de agent/ sem o pacote
    from core import browser_task_state as bts, browser_turn_context as btc

logger = logging.getLogger(__name__)

UNAVAILABLE = "O navegador não respondeu agora."
EVENT_WAIT_SECONDS = 20.0
NOTICE = {
    "question": "O navegador precisa de uma resposta sua para continuar: {text}",
    "done": "Terminei a tarefa no navegador: {text}",
    "failed": "A tarefa no navegador parou: {text}",
}
CANCEL = re.compile(r"\b(cancela\w*|esquece|deixa (pra|para) la|para (a|essa) tarefa|pare|parar a tarefa|desiste)\b")
SPEAK_ATTEMPTS = 25  # ~25 s tentando: a pergunta sai assim que a Kira termina a frase anterior
SPEAK_RETRY_SECONDS = 1.0
SECRET_WAIT_SECONDS = 2.5  # a ferramenta do Gemini chega antes da transcrição da fala (que o guarda transforma)
ALREADY_ANSWERED = "Já passei a resposta para o navegador."

JsonGetter = Callable[[str, float], Awaitable[dict]]
Speaker = Callable[[str], Awaitable[bool]]


def _fold(text: object) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").lower())
    return " ".join("".join(ch for ch in raw if not unicodedata.combining(ch)).split())


async def _get_local(path: str, read_timeout: float = 3.0) -> dict:
    token = str(os.getenv("KIRA_INTERNAL_TOKEN") or "").strip()
    timeout = httpx.Timeout(connect=0.5, read=read_timeout, write=0.5, pool=0.5)
    async with httpx.AsyncClient(timeout=timeout, headers={"X-Kira-Internal": token}) as client:
        response = await client.get(_local_backend_url(path))
    response.raise_for_status()
    body = response.json()
    return (body.get("data") or {}) if isinstance(body, dict) else {}


async def browser_watch_loop(*, speak: Speaker, get_json: JsonGetter = _get_local, interval: float = 3.0,
                             stop_event: asyncio.Event | None = None) -> None:
    """Estado real da aba para o atalho de voz e fala das novidades da tarefa (pergunta, pronto, parou).

    A aba conta como aberta com o navegador ligado e a imagem ao vivo sendo vista; "fechada" só depois de duas
    leituras seguidas (a aba leva meio segundo para conectar a imagem). Eventos antigos não são repetidos."""
    after: int | None = None
    closed_reads = 0
    while stop_event is None or not stop_event.is_set():
        try:
            state = await get_json("/api/browser/state", 3.0)
            task = state.get("task") or {}
            if after is None:
                after = int(task.get("seq") or 0)
            if state.get("open") and int(state.get("viewers") or 0) > 0:
                closed_reads = 0
                if not btc.is_browser_tab_open():
                    btc.set_browser_tab_open(True)
            else:
                closed_reads += 1
                if closed_reads >= 2 and btc.is_browser_tab_open():
                    btc.set_browser_tab_open(False)
            bts.set_browser_task_waiting(task.get("state") == "waiting", secret=bool(task.get("secret")))
            if task.get("active"):
                data = await get_json(f"/api/browser/task/events?after={after}&wait={EVENT_WAIT_SECONDS:.0f}", EVENT_WAIT_SECONDS + 5.0)
                for event in data.get("events") or []:
                    after = max(after, int(event.get("seq") or 0))
                    template = NOTICE.get(str(event.get("kind") or ""))
                    if template is None:
                        continue
                    still_needed = None
                    if event.get("kind") == "question":
                        bts.set_browser_task_waiting(True, secret=bool(event.get("secret")))
                        still_needed = bts.is_browser_task_waiting  # respondida pela tela: para de insistir
                    await _speak_with_retry(speak, template.format(text=str(event.get("text") or "")[:300]),
                                            still_needed=still_needed)
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("BROWSER_WATCH_SKIPPED reason=%s", type(exc).__name__)
        if stop_event is None:
            await asyncio.sleep(interval)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


async def _speak_with_retry(speak: Speaker, message: str, *, still_needed: Callable[[], bool] | None = None,
                            retry_seconds: float = SPEAK_RETRY_SECONDS) -> None:
    """A fala proativa pode ser recusada (Kira falando, voz se recuperando): tenta de novo algumas vezes, enquanto
    ainda fizer sentido (pergunta já respondida pela tela não é repetida)."""
    for attempt in range(SPEAK_ATTEMPTS):
        if still_needed is not None and not still_needed():
            return
        try:
            if await speak(message):
                return
        except Exception as exc:
            logger.debug("BROWSER_NOTICE_SPEAK_FAILED reason=%s", type(exc).__name__)
        if attempt + 1 < SPEAK_ATTEMPTS:
            await asyncio.sleep(retry_seconds)
    logger.info("BROWSER_NOTICE_NOT_SPOKEN")


class BrowserToolsMixin:
    async def _browser_start_task(self, goal: str, url: str = "") -> str:
        """Começa a tarefa no backend e abre a aba Internet para o Chefe ver. Devolve a frase de confirmação."""
        try:
            result = await _post_local("/api/browser/task", {"goal": goal, "url": url}, read_timeout=8.0)
        except (httpx.HTTPError, TimeoutError):
            return UNAVAILABLE
        if not result.get("success"):
            return UNAVAILABLE
        try:
            await self._dispatch_kira_ui_action("open_tab", target="browser")  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("BROWSER_TASK_OPENTAB_FAILED reason=%s", type(exc).__name__)
        btc.set_browser_tab_open(True)
        logger.info("BROWSER_TASK_REQUESTED")
        return "Comecei no navegador, aqui na aba Internet. Se precisar de algo, eu te pergunto."

    async def _browser_answer(self, answer: str) -> str:
        """Resposta do Chefe ao que o navegador perguntou ("cancela" para a tarefa). Senha falada chega aqui como o
        marcador (o guarda da transcrição guardou o texto): ela vai para a tarefa e a Kira confirma só o tamanho."""
        placeholder = answer.strip() == bts.SECRET_PLACEHOLDER
        if not bts.is_browser_task_waiting() and bts.answered_recently(answer):
            return ALREADY_ANSWERED  # a mesma fala já foi pelo outro caminho
        secret_expected = bts.is_browser_secret_expected()
        spoken_secret = bts.pop_spoken_secret() if secret_expected else ""
        if secret_expected and not spoken_secret and not placeholder:
            # veio da ferramenta do Gemini (a versão dele da senha): espera a fala que o guarda guardou, que é a usada
            for _ in range(int(SECRET_WAIT_SECONDS / 0.1)):
                await asyncio.sleep(0.1)
                if not bts.is_browser_task_waiting():
                    return ALREADY_ANSWERED  # o atalho respondeu com a fala enquanto esperava
                spoken_secret = bts.pop_spoken_secret()
                if spoken_secret:
                    break
            spoken_secret = spoken_secret or answer
        bts.set_browser_task_waiting(False)
        if placeholder and not spoken_secret:
            bts.set_browser_task_waiting(True, secret=secret_expected)
            return "Não peguei a senha. Fala de novo, por favor?"
        # marca já (não depois do envio): o outro caminho chega com a mesma fala enquanto esta resposta ainda vai
        bts.note_answered(bts.SECRET_PLACEHOLDER if spoken_secret else answer)
        try:
            if not spoken_secret and CANCEL.search(_fold(answer)):
                await _post_local("/api/browser/task/stop", {}, read_timeout=5.0)
                return "Parei a tarefa no navegador."
            payload = {"answer": spoken_secret, "spoken": True} if spoken_secret else {"answer": answer}
            result = await _post_local("/api/browser/task/answer", payload, read_timeout=5.0)
        except (httpx.HTTPError, TimeoutError):
            return UNAVAILABLE
        data = result.get("data") or {}
        if not data.get("ok"):
            return "O navegador não está esperando resposta agora."
        if data.get("secret_chars"):
            # o formato (sem a senha) deixa o Chefe perceber na hora se foi ouvida errado
            shape = data.get("secret_shape") or f"{int(data['secret_chars'])} caracteres"
            return f"Anotei a senha, {shape}. Continuando."
        return "Certo, continuando."

    @function_tool(
        name="navegador_tarefa",
        description=(
            "Faz uma tarefa de várias etapas no navegador da Kira, que o Chefe vê ao vivo na aba Internet: preencher "
            "formulário, criar conta, fazer login, procurar algo dentro de um site, ver o que uma página mostra. "
            "objetivo: o que o Chefe pediu, com os dados que ele já disse (nome, e-mail...). site: endereço ou nome do "
            "site, se ele disse. Roda em segundo plano: diga só que começou. Quando faltar algo, o navegador pergunta "
            "e você repassa a pergunta; a resposta do Chefe vai em navegador_responder. Também para: abrir um site ('abre o g1', "
            "'entra no YouTube': objetivo 'abrir o site' e site = o nome/endereço), comparar preços em lojas ('os 3 fones mais "
            "baratos no Mercado Livre'), baixar um arquivo de um site ('baixa o livro X em EPUB no Gutenberg') e fazer prova "
            "ou questionário online."
        ),
    )
    async def navegador_tarefa(self, objetivo: str, site: str = "") -> str:
        return await self._browser_start_task(objetivo, site)

    @function_tool(
        name="navegador_responder",
        description=(
            "Passa ao navegador a resposta do Chefe para a pergunta que você repassou (um dado, uma escolha, 'pode "
            "enviar', 'pronto' depois de ele resolver algo na tela). Use logo depois que ele responder. Também quando, "
            "em vez de responder, ele mandar fazer algo na página ('aperta em esqueceu a senha', 'não sei a senha', "
            "'clica naquele anúncio'): passe a ordem como está, que o navegador faz."
        ),
    )
    async def navegador_responder(self, resposta: str) -> str:
        return await self._browser_answer(resposta)

    @function_tool(name="navegador_parar", description="Para a tarefa que está rodando no navegador da Kira.")
    async def navegador_parar(self) -> str:
        try:
            await _post_local("/api/browser/task/stop", {}, read_timeout=5.0)
        except (httpx.HTTPError, TimeoutError):
            return UNAVAILABLE
        bts.set_browser_task_waiting(False)
        return "Parei a tarefa no navegador."


__all__ = ["BrowserToolsMixin", "browser_watch_loop", "NOTICE"]
