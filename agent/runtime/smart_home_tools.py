"""Tool 'casa' (services/smart_home_api.py): aparelhos do Wi-Fi de casa e controle da TV LG / Chromecast."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local

logger = logging.getLogger(__name__)


class SmartHomeToolsMixin:
    async def _tv_say(self, message: str) -> None:
        """Fala da Kira fora do turno (progresso e resultado da tarefa na TV)."""
        coordinator = getattr(self, "_speech_coordinator", None)
        try:
            spoken = coordinator is not None and await coordinator.speak_proactive_message(message, label="TV", add_to_chat_ctx=False)
        except Exception:  # noqa: BLE001 - fala é extra: nunca derruba a tarefa
            spoken = False
        logger.info("TV_TAREFA_FALA spoken=%s", bool(spoken))

    async def _tv_task(self, aparelho: str, valor: str) -> None:
        """'tarefa' na TV em segundo plano (2026-11-07: antes segurava a ferramenta >1 min, calada): avisa se passar de
        25 s e confirma o resultado em voz quando termina."""
        done = asyncio.Event()

        async def progress() -> None:
            try:
                await asyncio.wait_for(done.wait(), 25)
            except asyncio.TimeoutError:
                await self._tv_say("Ainda estou procurando na TV, só mais um pouco.")

        watcher = asyncio.create_task(progress())
        try:
            result = await _post_local("/api/ui/casa", {"action": "tarefa", "device": aparelho, "value": valor}, read_timeout=150.0)
        except (httpx.HTTPError, TimeoutError, ValueError):
            result = {"ok": False, "fala": "Não consegui falar com a TV."}
        finally:
            done.set()
            watcher.cancel()
        ok = bool(result.get("ok"))
        await self._tv_say(str(result.get("fala") or ("Pronto, já está passando na TV." if ok else "Não consegui colocar na TV.")))

    @function_tool(
        name="casa",
        description=(
            "Casa inteligente pelo Wi-Fi (os aparelhos que o Chefe adicionou). Use SEMPRE que o pedido citar TV, televisão, "
            "Chromecast ou caixa de som — nunca o volume do PC. aparelho: o nome ('TV da sala'), 'tv' ou vazio (= a TV dele). "
            "acao na TV LG: 'ligar', 'desligar', 'volume' (valor 0-100), 'aumentar', 'diminuir', 'mudo', 'desmutar', "
            "'youtube' (valor = música ou vídeo a buscar: 'coloca Charlie Brown na TV'), 'abrir' (valor = app: Netflix, Globoplay, "
            "Prime Video, Prime IPTV, Disney+, LG Channels, Alexa — o nome como ele disser), 'apps' (lista os apps instalados), "
            "'tela_kira' (valor = tema: orbe, minimo ou neon) põe a TELA DA KIRA em tela cheia na TV — use para 'coloca a tela "
            "da Kira / sua tela / a Kira na TV'; 'restaurar_tela' volta ao que estava antes ('tira da TV'). Para MANDAR painel, "
            "música ou algo para a TV ('joga pra TV') use a ferramenta tv_tela acao='mostrar'. "
            "'tv' (TV ao vivo da ANTENA/cabo; NUNCA para a tela da Kira), 'canal' (valor = número), 'canal+', 'canal-', 'lista', 'info', 'menu', 'sair', '0'..'9', "
            "'o que esta passando', 'pausar', 'tocar' (pausar/continuar o que passa na TV: SEMPRE esta ação direta, rápida — "
            "nunca 'tarefa'; ela já sabe quantos OK cada app pede), 'voltar', 'home', 'ok', 'cima', 'baixo', 'esquerda', 'direita'. "
            "'tarefa' (valor = o objetivo inteiro, ex.: 'abrir Como Eu Conheci Sua Mãe no Prime IPTV e continuar de onde parou'): a Kira olha a tela da TV e aperta os botões sozinha até chegar — use para filme, série, canal ou qualquer coisa dentro de um app (Prime IPTV, Netflix...). Roda em segundo plano: diga só 'vou colocar, um instante'; ela avisa sozinha se demorar e confirma em voz quando terminar (não diga que já está passando). No Chromecast: 'pausar', 'tocar', 'parar', 'volume', 'mudo'. 'controle' abre na TELA DA KIRA o painel Casa com o controle remoto (use para 'abre o controle da TV'; 'apps' só lista os apps instalados na TV). Outras: 'listar' "
            "(aparelhos do Chefe), 'procurar' (olhar a rede), 'renomear' (valor = nome novo). Só confirme uma ação de aparelho quando o JSON trouxer ok=true e confirmed=true; "
            "ok=false ou confirmed=false significa que nada foi confirmado. Fale curto."
        ),
    )
    async def casa(self, acao: str = "listar", aparelho: str = "", valor: str = "") -> str:
        if str(acao or "").strip().lower() == "tarefa":
            self._tv_task_handle = asyncio.create_task(self._tv_task(aparelho, valor))
            return json.dumps({"ok": True, "confirmed": False, "em_andamento": True,
                               "fala": "Diga AGORA, curto: 'vou colocar, um instante'. O resultado ela confirma sozinha em voz quando terminar; não diga que já está passando."},
                              ensure_ascii=False)
        if str(acao or "").strip().lower() in {"controle", "painel", "abrir_controle", "abrir_painel"}:
            # "abre o controle da TV" = painel Casa da Kira na tela, não a lista de apps da TV
            try:
                r = await _post_local("/api/ui/actions", {"type": "ui_control", "command": "open_tab", "value": "casa"})
            except (httpx.HTTPError, TimeoutError, ValueError):
                return json.dumps({"ok": False, "confirmed": False, "message": "Não consegui falar com a tela da Kira."}, ensure_ascii=False)
            shown = bool(r.get("ok")) and int(r.get("delivered") or 0) > 0
            return json.dumps({"ok": shown, "confirmed": shown, "message": "Painel Casa (controle) enviado à tela." if shown else "Nenhuma tela da Kira conectada: o controle NÃO apareceu."}, ensure_ascii=False)
        try:
            result = await _post_local("/api/ui/casa", {"action": acao, "device": aparelho, "value": valor}, read_timeout=150.0)
        except (httpx.HTTPError, TimeoutError, ValueError):
            return "Não consegui olhar a rede agora."
        return json.dumps(result, ensure_ascii=False)


    @function_tool(
        name="tv_tela",
        description=(
            "A TV LG como TELA EXTRA da Kira (página em tela cheia no navegador da TV, com o orbe que fala junto). "
            "acao: 'ativar' = A KIRA VAI PRA TV ('vai pra TV', 'coloca a tela da Kira / sua tela na TV'): abre a tela dela na TV, "
            "TUDO passa a aparecer lá (painéis, Olho de Deus, música, imagens) e a VOZ sai pela TV; a resposta só volta quando a TV "
            "estiver pronta, então NÃO diga 'pronto' antes (diga no máximo 'indo pra TV'). Depois disso: 'mostra X no computador' = "
            "abre X e usa move_panel(window='principal') (ela continua na TV); 'fala pelo computador' = voz_na_tv(ligar=false) "
            "(continua na TV); 'volta pro PC' = acao 'soltar' (tudo e a voz voltam). 'mostrar' (MANDA algo "
            "para a TV: 'joga esse painel pra TV', 'manda essa música pra televisão', 'passa a imagem da carta pra TV'; o_que = "
            "'ultimo_painel' (o último aberto) | 'musica' | o NOME/assunto do painel ('imagem da carta', 'clima', 'simulado') — "
            "leva o painel igualzinho ao do PC, com imagens; 'texto: ...' só se ele pedir para escrever algo na TV); antes de mandar ela confere sozinha se a TV está na "
            "página dela e abre se não estiver; 'modelo' (n = 1 a 5, troca o visual da tela da TV); 'restaurar' ('tira da "
            "TV', 'sai da TV', 'não mostra sua tela na TV', 'restaura a TV': fecha e volta ao que estava antes; a TV sai do sorteio e a "
            "voz volta ao PC sozinha. 'FECHA O PAINEL (da TV)', 'fecha tudo, a TV também', 'fecha a música' NÃO são restaurar: use "
            "close_panels / control_music e a tela da Kira continua na TV); "
            "'rolar' (o_que = 'baixo' | 'cima' | 'topo': 'rola pra baixo', 'desce mais', 'sobe' no painel em destaque da TV, "
            "quando nem encolhendo ele coube); 'fixar' (os PRÓXIMOS painéis vão todos para a TV por 10 min: 'mostra os painéis na TV', 'mostra na TV'); "
            "'soltar' ('volta a mostrar aqui', 'para de mandar pra TV'). Pedido que mistura conteúdo + 'na TV' ('mostra o "
            "briefing/clima/notícias na TV') = PRIMEIRO tv_tela acao='fixar', DEPOIS a ferramenta do conteúdo. "
            "'olho_de_deus' (abre o MESMO Olho de Deus do PC NA TV: 'abre/passa/joga o Olho de Deus pra TV'; o_que='fechar' "
            "tira; depois disso 'foca em X', camadas, mapa etc. usam a ferramenta normal do Olho de Deus e acontecem NA TV). "
            "'principal' ('mostra só na TV a partir de agora', 'usa a TV como tela principal'): TUDO (painéis, estudos, "
            "imagens, clima, Olho de Deus, webcam) vai para a TV até ele pedir 'soltar'/'volta pro PC'. "
            "Painel que já está na TV (veja o estado das telas) se altera lá: atualize o mesmo, não crie outro no PC. Abas: música vai para a TV com mostrar o_que='musica'; WhatsApp, "
            "e-mail e dados pessoais NUNCA vão para a TV. "
            "'limites'. Com a TV ativa, os painéis são sorteados entre a tela principal, a janela extra e a TV. A LG não "
            "deixa mudar o menu do sistema. Na 1ª vez a TV pode pedir para aceitar a Kira: peça ao Chefe. "
            "Só confirme quando vier ok=true."
        ),
    )
    async def tv_tela(self, acao: str = "ativar", o_que: str = "ultimo_painel", n: int = 1, tema: str = "orbe", aparelho: str = "tv", camadas: str = "") -> str:
        acao = str(acao or "").strip().lower()
        if acao == "limites":
            return ("A LG não deixa trocar o menu nem o visual do sistema. Dá para abrir a tela da Kira em tela cheia no "
                    "navegador da TV (5 modelos), mandar painéis e música para ela e depois restaurar o que estava passando.")
        try:
            if acao in {"pc", "volta_pc", "voltar"}:
                acao = "soltar"
            if acao in {"mostrar", "modelo", "fixar", "soltar", "olho_de_deus", "principal", "so_tv", "rolar"}:
                if acao == "olho_de_deus" and o_que == "ultimo_painel":
                    o_que = "abrir"
                body = {"o_que": o_que, "aparelho": aparelho, "acao": acao, "camadas": camadas, **({"modelo": n} if acao == "modelo" else {})}
                result = await _post_local("/api/tv/mostrar", body, read_timeout=60.0)
            else:
                action = "restaurar_tela" if acao == "restaurar" else "tela_kira"
                result = await _post_local("/api/ui/casa", {"action": action, "device": aparelho, "value": tema}, read_timeout=60.0)
                if action == "restaurar_tela":  # tirou a TV: os próximos painéis voltam para cá
                    await _post_local("/api/tv/mostrar", {"acao": "soltar"}, read_timeout=5.0)
                elif result.get("ok"):  # "vai pra TV": a Kira inteira muda (painéis + Olho de Deus + voz), não só a página
                    main = await _post_local("/api/tv/mostrar", {"acao": "principal", "aparelho": aparelho}, read_timeout=40.0)
                    voice = await _post_local("/api/tv/audio", {"ligar": True, "aparelho": aparelho}, read_timeout=40.0)
                    result = {"ok": bool(main.get("ok")), "confirmed": bool(main.get("ok")), "voz_na_tv": bool(voice.get("ok")),
                              "fala": "" if voice.get("ok") else (voice.get("fala") or "A voz continua no PC.")}
        except (httpx.HTTPError, TimeoutError, ValueError):
            return "Não consegui falar com a TV agora."
        return json.dumps(result, ensure_ascii=False)


__all__ = ["SmartHomeToolsMixin"]


_WAKE_RE = re.compile(r"\b(kira|quira|aquira)\b", re.IGNORECASE)
_duck_state = {"at": 0.0, "task": None}


def duck_tv_on_wake(text: str) -> None:
    """Chefe chamou "Kira" com mídia tocando na TV: abaixa o volume da TV e devolve em 25 s.
    O serviço só mexe se a própria Kira pôs mídia na TV nas últimas horas (senão nem conecta).
    KIRA_TV_DUCK=0 desliga."""
    if os.getenv("KIRA_TV_DUCK", "1").strip().lower() in {"0", "false", "off", "no"} or not _WAKE_RE.search(str(text or "")):
        return
    now = time.monotonic()
    if now - _duck_state["at"] < 20.0:  # um pedido por chamada (as parciais repetem o nome)
        return
    _duck_state["at"] = now

    async def run() -> None:
        try:
            result = await _post_local("/api/ui/casa", {"action": "abaixar_para_ouvir", "device": "tv"}, read_timeout=8.0)
            if not result.get("confirmed"):
                return
            await asyncio.sleep(25.0)
            await _post_local("/api/ui/casa", {"action": "restaurar_volume", "device": "tv"}, read_timeout=8.0)
        except Exception:  # noqa: BLE001 - TV fora do ar: só não abaixa
            return

    try:
        _duck_state["task"] = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return
