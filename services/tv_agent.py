"""A Kira enxergando a TV: tira foto da tela, o Gemini (visão) escolhe os botões, ela aperta e olha de novo.

"Coloca Como Eu Conheci Sua Mãe no Prime IPTV, continua de onde parei" vira passos como os de uma pessoa com o controle:
abrir o app, ir em Séries, achar o pôster, Assistir, "Continuar de S7 E2". Testado em 2026-11-01 (a série tocou).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
from typing import Any

import httpx

logger = logging.getLogger(__name__)
MODEL = os.getenv("KIRA_TV_VISION_MODEL", "gemini-2.5-flash")
MAX_STEPS = 25
BUTTONS = {"UP", "DOWN", "LEFT", "RIGHT", "ENTER", "BACK", "HOME", "EXIT", "CHANNELUP", "CHANNELDOWN", *"0123456789"}
PROMPT = """Você controla uma TV LG pelo controle remoto, olhando a foto da tela. Objetivo do Chefe: {goal}
Já feito: {history}
Responda só JSON: {{"botoes": ["RIGHT", "ENTER"], "abrir": "", "texto": "", "pronto": false, "fala": ""}}
- botoes: até 4 de UP, DOWN, LEFT, RIGHT, ENTER, BACK, HOME, EXIT, CHANNELUP, CHANNELDOWN, 0-9. Veja onde está o destaque
  (item maior, mais claro ou com borda) e conte quantas casas andar até o alvo antes de apertar ENTER.
- abrir: nome de um app instalado para abrir direto (ex.: "Prime IPTV"). Se o app do objetivo não está na tela, use
  abrir primeiro (nunca navegue pela tela inicial da TV).
- texto: o que digitar se houver um campo de busca aberto e selecionado.
- Prefira "Continue assistindo"/"Continuar de onde parou" quando o Chefe pedir para continuar.
- Player com o vídeo pedido na tela (com ou sem barra de tempo): pronto=true e botoes vazio — a Kira confere sozinha se
  está pausado. Nunca LEFT/RIGHT ali (adiantam o vídeo).
- pronto: true quando o objetivo estiver na tela (o vídeo pedido aberto no player); a Kira confere se está andando.
  Com pronto=true, botoes vazio. fala: frase curta do resultado.
- Se travar ou não achar, pronto=true e fala explicando."""


async def _shot(client: Any) -> bytes:
    for attempt in range(2):  # a TV às vezes demora a entregar a foto: mais uma vez
        try:
            found = await client.request("tv/executeOneShot", {"path": "/tmp/capture.png", "method": "DISPLAY", "format": "PNG"})
            async with httpx.AsyncClient(timeout=15, verify=False) as http:  # a TV serve a foto com certificado próprio
                return (await http.get(found["imageUri"])).content
        except httpx.HTTPError:
            if attempt:
                raise
            await asyncio.sleep(2)
    return b""


def _dark(jpeg: bytes) -> bool:
    from io import BytesIO

    from PIL import Image, ImageStat

    return ImageStat.Stat(Image.open(BytesIO(jpeg)).convert("L").resize((64, 36))).mean[0] < 10


async def _still(client: Any) -> bool:
    """Duas fotos com 2 s de diferença quase iguais: a imagem não anda (vídeo pausado)."""
    from io import BytesIO

    from PIL import Image, ImageChops

    small = lambda data: Image.open(BytesIO(data)).convert("L").resize((96, 54))
    first = small(await _shot(client))
    await asyncio.sleep(2)
    diff = ImageChops.difference(first, small(await _shot(client)))
    return sum(diff.getdata()) / (96 * 54) < 2.0


async def _decide(genai: Any, jpeg: bytes, goal: str, history: list[str]) -> dict[str, Any]:
    from google.genai import types

    for model in (MODEL, "gemini-flash-latest", "gemini-2.5-flash-lite"):  # 503 "muita demanda": tenta o próximo
        try:
            reply = await genai.aio.models.generate_content(
                model=model, contents=[types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"), PROMPT.format(goal=goal, history="; ".join(history[-8:]) or "nada")],
                # sem "pensar" (2026-11-07): cada passo levava vários segundos a mais e a tarefa passava de 1 min
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1,
                                                   **({"thinking_config": types.ThinkingConfig(thinking_budget=0)} if model.startswith("gemini-2.5") else {})))
            break
        except Exception as exc:  # noqa: BLE001 - modelo fora do ar
            logger.info("TV_AGENT_MODEL_FAILED model=%s reason=%s", model, type(exc).__name__)
    else:
        return {"pronto": False, "falha": True, "fala": "O Gemini não respondeu agora; tente de novo em instantes."}
    try:
        plan = json.loads(reply.text or "{}")
        return plan if isinstance(plan, dict) else {"pronto": False, "falha": True, "fala": "A resposta visual da TV veio em formato inválido."}
    except ValueError:
        return {"pronto": False, "falha": True, "fala": "A resposta visual da TV veio em formato inválido."}


async def run(client: Any, goal: str) -> dict[str, Any]:
    """client: WebOsClient já conectado. Devolve {ok, fala, passos}."""
    from google import genai as google_genai

    genai = google_genai.Client(api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))
    history: list[str] = []
    original_goal = " ".join(str(goal or "").split())[:500]
    effective_goal = original_goal
    learned_memory = None
    try:
        from services.experience_memory import get_experience_memory

        learned_memory = get_experience_memory()
        hints = learned_memory.recommend(original_goal, device="TV LG webOS", limit=3)
        # Preferência contextual só complementa o pedido; nunca substitui um
        # aplicativo explicitamente citado na frase atual.
        folded_goal = unicodedata.normalize("NFKD", original_goal).encode("ascii", "ignore").decode().lower()
        if not any(str(pref.get("context", {}).get("app") or "").lower() in folded_goal for pref in hints.get("preferences") or []):
            for pref in hints.get("preferences") or []:
                app_hint = str((pref.get("context") or {}).get("app") or "").strip()
                task_hint = str((pref.get("context") or {}).get("task_type") or "").strip()
                if app_hint and (not task_hint or task_hint in folded_goal or (task_hint == "series" and "serie" in folded_goal)):
                    effective_goal = f"{original_goal}. Preferência contextual do Chefe: usar {app_hint}."
                    history.append(f"preferência contextual: {app_hint}")
                    break
        best = hints.get("best") or {}
        if best.get("success_count") and best.get("steps"):
            history.append("estratégia anterior comprovada: " + "; ".join(str(step) for step in best.get("steps", [])[:6]))
    except Exception as exc:
        logger.info("TV_AGENT_MEMORY_HINTS_SKIPPED reason=%s", type(exc).__name__)
    # O app citado no pedido ("no Prime IPTV") abre direto pelo código: o Gemini só navega dentro dele
    key = lambda text: re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", str(text).lower()).encode("ascii", "ignore").decode())
    apps = [a for a in (await client.get_apps_all()) or [] if a.get("title") and a.get("visible", True)]
    named = max((a for a in apps if len(key(a["title"])) > 3 and key(a["title"]) in key(original_goal)), key=lambda a: len(a["title"]), default=None)
    if named and str(await client.get_current_app() or "") != named["id"]:
        await client.launch_app(named["id"])
        history.append(f"abriu {named['title']}")
        await asyncio.sleep(4)
    for step in range(MAX_STEPS):
        jpeg = await _shot(client)
        for _ in range(4):  # foto quase preta = tela carregando (app abrindo, vídeo começando): espera e olha de novo
            if not _dark(jpeg):
                break
            await asyncio.sleep(1.2)
            jpeg = await _shot(client)
        plan = await _decide(genai, jpeg, effective_goal, history)
        logger.info("TV_AGENT_STEP step=%s plan=%s", step, json.dumps(plan, ensure_ascii=False)[:300])
        if plan.get("falha"):
            result = {"ok": False, "fala": plan.get("fala") or "Não consegui interpretar a tela da TV.", "passos": step}
            if learned_memory is not None:
                learned_memory.record_attempt(
                    goal=original_goal,
                    status="failure",
                    confirmed=True,
                    app=str(named.get("title") if named else ""),
                    device="TV LG webOS",
                    context={"app": str(named.get("title") if named else ""), "device": "TV LG webOS"},
                    steps=history,
                    tools=["tv_agent", "screenshot", "gemini_vision"],
                    error=result["fala"],
                    evidence="planejamento visual indisponível; execução interrompida",
                    source="tv_agent",
                )
            return result
        if plan.get("pronto") and not plan.get("botoes"):  # "pronto" com botões: aperta e confere de novo
            for _ in range(2):  # uma foto não diz se o vídeo anda: duas fotos iguais = pausado → OK (no Prime IPTV o play
                if not await _still(client):  # geral da TV não tira do pause; o OK sim)
                    break
                await client.button("ENTER")
                await asyncio.sleep(2)
            result = {"ok": True, "fala": plan.get("fala") or "Pronto.", "passos": step}
            if learned_memory is not None:
                learned_memory.record_attempt(
                    goal=original_goal,
                    status="success",
                    confirmed=True,
                    app=str(named.get("title") if named else ""),
                    device="TV LG webOS",
                    context={"app": str(named.get("title") if named else ""), "device": "TV LG webOS"},
                    steps=history + [f"confirmou reprodução após {step} etapa(s)"],
                    tools=["tv_agent", "screenshot", "gemini_vision"],
                    evidence=result.get("fala") or "vídeo confirmado visualmente",
                    source="tv_agent",
                )
            return result
        if plan.get("abrir"):
            apps = [a for a in (await client.get_apps_all()) or [] if a.get("title")]
            app = next((a for a in apps if str(plan["abrir"]).lower() in a["title"].lower()), None)
            if app:
                await client.launch_app(app["id"])
                history.append(f"abriu {app['title']}")
                await asyncio.sleep(3.5)
                continue
        if plan.get("texto"):
            await client.request("com.webos.service.ime/insertText", {"text": str(plan["texto"])[:60], "replace": 0})
            history.append(f"digitou {plan['texto']}")
        pressed = [b for b in plan.get("botoes") or [] if b in BUTTONS][:4]
        for button in pressed:
            await client.button(button)
            await asyncio.sleep(0.4)
        history.append(" ".join(pressed) or "esperou")
        await asyncio.sleep(1.6 if "ENTER" in pressed else 0.5)  # tela nova carregando
    result = {"ok": False, "fala": "Não consegui chegar lá na TV; parei para não ficar apertando à toa.", "passos": MAX_STEPS}
    if learned_memory is not None:
        learned_memory.record_attempt(
            goal=original_goal,
            status="failure",
            confirmed=True,
            app=str(named.get("title") if named else ""),
            device="TV LG webOS",
            context={"app": str(named.get("title") if named else ""), "device": "TV LG webOS"},
            steps=history,
            tools=["tv_agent", "screenshot", "gemini_vision"],
            error=result["fala"],
            evidence="limite de etapas atingido; execução interrompida",
            source="tv_agent",
        )
    return result


__all__ = ["run"]
