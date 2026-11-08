"""HOTFIX 3.5 — contexto de propriedade do turno pelo Browser Agent novo.

Quando o roteador novo do navegador assume o turno (prelock / rota selecionada),
marcamos `browser_agent_owns_turn`. Qualquer caminho LEGADO (pc_control/open_web,
navegador externo, os.startfile/webbrowser) deve checar isto e ABORTAR — o unico
navegador permitido e o Browser Agent novo.

Flag em memoria do processo do agent (onde pc_control e o assistant rodam), com
expiracao curta para nao bloquear aberturas legitimas de turnos posteriores.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("kira.browser_turn_context")

_lock = threading.RLock()
_owns_until: float = 0.0
_owns_source: str = ""

# Janela de propriedade: cobre o intervalo entre prelock/route_selected e a
# eventual chamada legada de pc_control no mesmo turno (observado ~2s depois).
_OWN_WINDOW_S = 30.0


def set_browser_agent_owns_turn(source: str = "unknown") -> None:
    global _owns_until, _owns_source
    with _lock:
        _owns_until = time.monotonic() + _OWN_WINDOW_S
        _owns_source = str(source or "unknown")
    logger.info("BROWSER_AGENT_OWNS_TURN_SET source=%s window_s=%s", source, int(_OWN_WINDOW_S))


def browser_agent_owns_turn() -> bool:
    with _lock:
        return time.monotonic() < _owns_until


def clear_browser_agent_owns_turn() -> None:
    global _owns_until
    with _lock:
        _owns_until = 0.0
    logger.info("BROWSER_AGENT_OWNS_TURN_CLEARED")


# ITEM 3 — cache do status REAL do Browser Agent (online + screenshot recente).
# Setado de forma assincrona quando o agente confirma o estado; lido de forma
# SINCRONA pelo filtro de transcript (que nao pode fazer HTTP). TTL curto: so
# consideramos "vivo" se confirmado ha pouco tempo.
_runtime_alive_until: float = 0.0


def set_browser_runtime_alive(alive: bool, *, ttl_s: float = 20.0) -> None:
    global _runtime_alive_until
    with _lock:
        _runtime_alive_until = (time.monotonic() + float(ttl_s)) if alive else 0.0


def browser_runtime_alive_cached() -> bool:
    """True se o Browser Agent foi confirmado online (com screenshot) ha pouco tempo."""
    with _lock:
        return time.monotonic() < _runtime_alive_until


# ==========================================================================
# FASE 1 — flag de contexto: a aba "Internet" esta aberta com um site carregado?
# Vira True quando a aba abre (navegacao embutida), False quando fecha. Usada pelo
# dispatcher de 4 casos para baixar o threshold de deteccao (com a aba aberta,
# qualquer comando de controle conta: clica/desce/pausa/proximo...).
# ==========================================================================
_browser_tab_open: bool = False


def set_browser_tab_open(value: bool) -> None:
    global _browser_tab_open
    with _lock:
        _browser_tab_open = bool(value)
    logger.info("BROWSER_TAB_OPEN_SET value=%s", str(bool(value)).lower())


def is_browser_tab_open() -> bool:
    with _lock:
        return _browser_tab_open


# ==========================================================================
# PASSO 3 — BROWSER_MODE: estado explicito do fluxo Kira<->Browser Agent.
# Enquanto ativo, o Gemini Live fica bloqueado (PASSO 4) e o monitor loop
# (PASSO 5) conduz o fluxo. SALVAGUARDA: lifetime maximo absoluto -> mesmo se o
# monitor morrer/travar, o modo auto-desativa e o Gemini volta (nunca bloqueia
# para sempre). 'fase': confirmando | aguardando_inicio | monitorando | aguardando_resposta
# ==========================================================================
_BROWSER_MODE_MAX_LIFETIME_S = 900.0  # 15 min — teto de seguranca

_browser_mode: dict = {
    "ativo": False,
    "job_id": None,
    "fase": None,
    "contador_inicio": None,   # epoch (time.time) de quando o job foi mandado
    "ultimo_needs": None,
    "_activated_at": 0.0,       # monotonic — uso interno p/ a salvaguarda
}


def _reset_browser_mode_unlocked() -> None:
    _browser_mode.update({
        "ativo": False, "job_id": None, "fase": None,
        "contador_inicio": None, "ultimo_needs": None, "_activated_at": 0.0,
    })


def _browser_mode_stale_unlocked() -> bool:
    at = float(_browser_mode.get("_activated_at") or 0.0)
    return bool(_browser_mode.get("ativo")) and at > 0.0 and (time.monotonic() - at) > _BROWSER_MODE_MAX_LIFETIME_S


def activate_browser_mode(*, job_id=None, fase: str = "confirmando") -> None:
    with _lock:
        _browser_mode.update({
            "ativo": True, "job_id": job_id, "fase": fase,
            "contador_inicio": time.time(), "ultimo_needs": None,
            "_activated_at": time.monotonic(),
        })
    logger.info("BROWSER_MODE_ACTIVATED job_id=%s fase=%s", job_id, fase)


def deactivate_browser_mode(*, reason: str = "done") -> None:
    with _lock:
        was = bool(_browser_mode.get("ativo"))
        _reset_browser_mode_unlocked()
    if was:
        logger.info("BROWSER_MODE_DEACTIVATED reason=%s", reason)


def is_browser_mode_active() -> bool:
    with _lock:
        if not _browser_mode.get("ativo"):
            return False
        if _browser_mode_stale_unlocked():
            _reset_browser_mode_unlocked()
            logger.warning("BROWSER_MODE_AUTO_DEACTIVATED reason=stale_max_lifetime")
            return False
        return True


def browser_mode_snapshot() -> dict:
    """Copia segura do estado (sem o campo interno _activated_at)."""
    with _lock:
        return {k: v for k, v in _browser_mode.items() if not k.startswith("_")}


def browser_mode_fase() -> str | None:
    with _lock:
        return _browser_mode.get("fase") if _browser_mode.get("ativo") else None


def browser_mode_job_id():
    with _lock:
        return _browser_mode.get("job_id") if _browser_mode.get("ativo") else None


def set_browser_mode_fase(fase: str) -> None:
    with _lock:
        if _browser_mode.get("ativo"):
            _browser_mode["fase"] = fase
    logger.info("BROWSER_MODE_FASE fase=%s", fase)


def set_browser_mode_job(job_id) -> None:
    with _lock:
        if _browser_mode.get("ativo"):
            _browser_mode["job_id"] = job_id


def set_browser_mode_contador_inicio(ts=None) -> None:
    with _lock:
        if _browser_mode.get("ativo"):
            _browser_mode["contador_inicio"] = float(ts) if ts is not None else time.time()


def set_browser_mode_ultimo_needs(needs) -> None:
    with _lock:
        if _browser_mode.get("ativo"):
            _browser_mode["ultimo_needs"] = needs


def browser_mode_ultimo_needs():
    with _lock:
        return _browser_mode.get("ultimo_needs") if _browser_mode.get("ativo") else None


# --------------------------------------------------------------------------
# PASSO 3 — flag de "gate verificando" (classificacao em andamento). Bloqueia o
# Gemini ja no inicio da verificacao (antes do matched). TTL de seguranca para
# nunca ficar presa (se o gate crashar). Setada/limpa pelo router de comandos.
# --------------------------------------------------------------------------
_GATE_CHECKING_TTL_S = 90.0
_gate_checking_until: float = 0.0


def set_browser_gate_checking(value: bool, *, ttl_s: float = _GATE_CHECKING_TTL_S) -> None:
    global _gate_checking_until
    with _lock:
        _gate_checking_until = (time.monotonic() + float(ttl_s)) if value else 0.0
    logger.info("BROWSER_GATE_CHECKING set=%s", str(bool(value)).lower())


def is_browser_gate_checking() -> bool:
    with _lock:
        return time.monotonic() < _gate_checking_until


# PASSO 5b — decisao PURA do monitor loop (sem I/O, testavel). Dado o status do
# agente + o estado do browser_mode, retorna a acao que o loop deve executar.
_MONITOR_START_TIMEOUT_S = 60.0


def browser_monitor_decide(status: dict | None, fase: str, ultimo_needs,
                           contador_inicio: float, now: float) -> tuple:
    """Acoes possiveis:
      ('set_fase','monitorando')  -> agente comecou a trabalhar
      ('needs', needs)            -> agente precisa de algo NOVO (reformular+falar)
      ('finish', result, sucesso) -> concluido/erro (falar result + sair)
      ('start_timeout',)          -> passou 60s na fase aguardando_inicio sem 'trabalhando'
      ('noop',)                   -> nada a fazer neste tick
    """
    st = str((status or {}).get("status") or "")
    if st == "trabalhando":
        return ("set_fase", "monitorando") if fase == "aguardando_inicio" else ("noop",)
    if fase == "aguardando_inicio" and contador_inicio and (now - float(contador_inicio)) > _MONITOR_START_TIMEOUT_S:
        return ("start_timeout",)
    if st == "aguardando_resposta":
        needs = (status or {}).get("needs")
        return ("needs", needs) if needs != ultimo_needs else ("noop",)
    if st == "concluido":
        return ("finish", (status or {}).get("result"), True)
    if st == "erro":
        return ("finish", (status or {}).get("result"), False)
    return ("noop",)
