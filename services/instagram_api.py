"""Rotas do Instagram (services/instagram.py): a tool da Kira (token interno) monta a resposta e o painel em formato
de celular (perfil no meio, balões ligados por linhas: +3 seguidores, -1, mensagem de fulano, curtidas, reels)."""

from __future__ import annotations

import hmac
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Header, HTTPException

from services.controllers.research import ResearchController, SectionPlan
from services.image_proxy import proxied_url
from services.instagram import Instagram, NotLoggedIn

logger = logging.getLogger("kira.app.instagram")

PRESENTATION_ID = "instagram-kira"
LOGIN_URL = "https://www.instagram.com/accounts/login/"
ACTIONS = ("novidades", "mensagens", "seguidores_novos", "nao_seguem", "verificados", "buscar", "perfil", "login")
NOT_LOGGED = ("O Instagram não está logado no navegador da Kira. Abri a página de login na aba Internet: o Chefe entra "
              "com a conta dele uma vez (o Instagram não deixa a Kira digitar a senha) e depois é só pedir de novo.")


def _pic(url: str) -> str | None:
    try:
        return proxied_url(url) if url.startswith("https://") else None
    except Exception:
        return None


def _person(user: dict[str, Any]) -> dict[str, Any]:
    return {"username": user.get("username", ""), "name": user.get("name", ""), "verified": bool(user.get("verified")),
            "photoUrl": _pic(str(user.get("pic") or "")), "profileUrl": f"https://www.instagram.com/{user.get('username', '')}/"}


def _people(users: list[dict[str, Any]], limit: int = 30) -> list[dict[str, Any]]:
    return [_person(user) for user in users[:limit] if user.get("username")]


def _hour(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""


def _names(users: list[dict[str, Any]], limit: int = 5) -> list[str]:
    return [f"@{user['username']}" for user in users[:limit]]


def create_instagram_router(instagram: Instagram, research: ResearchController, *, token_provider: Callable[[], str],
                            open_login: Callable[[], Awaitable[Any]] | None = None) -> APIRouter:
    router = APIRouter()

    async def show(title: str, data: dict[str, Any], turn_id: str | None) -> None:
        async def load() -> dict[str, Any]:
            return {"data": data}

        await research.run(presentation_id=PRESENTATION_ID, title=title, turn_id=turn_id,
                           sections=[SectionPlan("instagram", "instagram", "Instagram", load)])

    def profile(me: dict[str, Any]) -> dict[str, Any]:
        return {"username": me["username"], "name": me["name"], "verified": me["verified"], "photoUrl": _pic(me["pic"]),
                "followers": me["followers"], "following": me["following"], "posts": me["posts"],
                "profileUrl": f"https://www.instagram.com/{me['username']}/"}

    async def act(body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or "").strip().lower()
        turn = body.get("turnId") if isinstance(body.get("turnId"), str) else None
        if action not in ACTIONS:
            return {"ok": False, "message": f"Ação desconhecida. Use: {', '.join(ACTIONS)}."}
        if action == "login":
            if open_login is not None:
                await open_login()
            return {"ok": True, "message": "Abri o login do Instagram na aba Internet. O Chefe entra com a conta dele lá, uma vez só."}
        me = await instagram.me()
        if action in ("novidades", "seguidores_novos", "mensagens"):
            news = await instagram.novidades()
            activity = news["atividade_hoje"]
            callouts = []
            if not news["primeira_vez"]:
                callouts.append({"label": "novos seguidores", "value": f"+{len(news['novos'])}", "tone": "good"})
                callouts.append({"label": "deixaram de seguir", "value": f"-{len(news['perdidos'])}", "tone": "bad" if news["perdidos"] else "calm"})
            for thread in news["nao_lidas"][:2]:
                callouts.append({"label": f"mensagem de {thread['name']}", "value": "✉", "tone": "info", "text": thread["text"]})
            for kind, label in (("curtida", "curtidas hoje"), ("comentario", "comentários hoje"), ("reel", "reels"), ("seguiu", "começaram a seguir")):
                if activity.get(kind):
                    callouts.append({"label": label, "value": str(activity[kind]), "tone": "info"})
            panel = {
                "profile": profile(me), "callouts": callouts[:8],
                "messages": [{"name": t["name"], "text": t["text"], "time": _hour(t["when"]), "unread": t["unread"]} for t in news["mensagens_hoje"][:8]],
                "people": _people(news["novos"], 12), "peopleTitle": "Novos seguidores" if news["novos"] else "",
                "note": "Primeira olhada: a partir de agora eu comparo e digo quem entrou e quem saiu." if news["primeira_vez"] else "",
            }
            await show("Instagram: novidades", panel, turn)
            return {
                "ok": True, "conta": f"@{me['username']}", "seguidores": me["followers"], "seguindo": me["following"],
                "primeira_vez": news["primeira_vez"],
                "novos_seguidores": _names(news["novos"], 8), "total_novos": len(news["novos"]),
                "deixaram_de_seguir": [f"@{u['username']}" for u in news["perdidos"][:8]], "total_perdidos": len(news["perdidos"]),
                "mensagens_nao_lidas": [{"de": t["name"], "texto": t["text"]} for t in news["nao_lidas"][:6]],
                "mensagens_hoje": [{"de": t["name"], "texto": t["text"], "hora": _hour(t["when"])} for t in news["mensagens_hoje"][:8]],
                "atividade_hoje": activity, "contagem_parcial": news["parcial"],
                "como_responder": ("Resuma em 2 ou 3 frases o que mudou (seguidores, mensagens, curtidas). Na primeira vez, diga que "
                                   "a partir de agora você acompanha quem entra e quem sai. O painel já mostra os detalhes."),
            }
        if action == "nao_seguem":
            users = await instagram.nao_seguem_de_volta()
            await show("Instagram: não te seguem de volta", {"profile": profile(me), "people": _people(users), "peopleTitle": f"Não te seguem de volta ({len(users)})",
                                                            "callouts": [{"label": "não seguem de volta", "value": str(len(users)), "tone": "bad"}]}, turn)
            return {"ok": True, "total": len(users), "alguns": _names(users, 10)}
        if action == "verificados":
            ver = await instagram.verificados()
            people = ver["seguindo"] + [u for u in ver["seguidores"] if u not in ver["seguindo"]]
            await show("Instagram: contas verificadas", {"profile": profile(me), "people": _people(people), "peopleTitle": "Verificados",
                                                        "callouts": [{"label": "verificados que você segue", "value": str(len(ver["seguindo"])), "tone": "info"},
                                                                     {"label": "verificados que te seguem", "value": str(len(ver["seguidores"])), "tone": "good"}]}, turn)
            return {"ok": True, "verificados_que_voce_segue": _names(ver["seguindo"], 15), "total_seguindo": len(ver["seguindo"]),
                    "verificados_que_te_seguem": _names(ver["seguidores"], 15), "total_seguidores": len(ver["seguidores"])}
        if action == "buscar":
            found = await instagram.buscar(str(body.get("name") or ""))
            name = str(body.get("name") or "").strip()
            await show(f"Instagram: {name}", {"profile": profile(me), "people": _people(found["busca"] or found["seguindo"]), "peopleTitle": f"Contas com \"{name}\"",
                                               "callouts": [{"label": "na busca", "value": str(len(found["busca"])), "tone": "info"},
                                                            {"label": "que você segue", "value": str(len(found["seguindo"])), "tone": "info"},
                                                            {"label": "que te seguem", "value": str(len(found["seguidores"])), "tone": "good"}]}, turn)
            return {"ok": True, "na_busca_do_instagram": len(found["busca"]), "voce_segue": _names(found["seguindo"], 10),
                    "te_seguem": _names(found["seguidores"], 10), "obs": "A busca do Instagram mostra no máximo umas 50 contas."}
        await show("Instagram", {"profile": profile(me), "callouts": []}, turn)
        return {"ok": True, "conta": f"@{me['username']}", "seguidores": me["followers"], "seguindo": me["following"], "posts": me["posts"]}

    async def guarded(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await act(body)
        except NotLoggedIn:
            if open_login is not None:
                try:
                    await open_login()
                except Exception:
                    pass
            return {"ok": False, "message": NOT_LOGGED}
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            logger.info("INSTAGRAM_FAILED action=%s reason=%s detail=%s", body.get("action"), type(exc).__name__, str(exc)[:160])
            return {"ok": False, "message": f"Não consegui ler o Instagram agora ({exc})."[:200]}

    @router.post("/api/ui/instagram")
    async def tool(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        expected = str(token_provider() or "")
        if not expected or not x_kira_internal or not hmac.compare_digest(str(x_kira_internal), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        return await guarded(payload if isinstance(payload, dict) else {})

    return router


__all__ = ["create_instagram_router", "LOGIN_URL"]
