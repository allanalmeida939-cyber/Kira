from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .local_memory_store import LocalMemoryStore, get_local_memory_store

logger = logging.getLogger(__name__)


_EXT_RE = re.compile(r"\.(txt|bat|py|html|css|js|json|md|csv|exe)\b", re.IGNORECASE)


@dataclass(frozen=True)
class OperationalMemoryIntent:
    query_type: str
    scope: str
    extension: str = ""
    target_name: str = ""
    time_window: str = "recent"


def _normalize_extension(text: str) -> str:
    m = _EXT_RE.search(text or "")
    if not m:
        return ""
    ext = m.group(1).lower()
    return f".{ext}"


def _is_today(ts: float) -> bool:
    if not ts:
        return False
    d = datetime.fromtimestamp(ts)
    now = datetime.now()
    return d.date() == now.date()


def detect_operational_memory_intent(text: str) -> OperationalMemoryIntent | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    normalized = " ".join(raw.lower().split())
    ext = _normalize_extension(normalized)

    if any(k in normalized for k in ("ultima sessao", "última sessão", "nessa sessao", "nesta sessao", "nessa sessão", "o que voce fez", "o que você fez")):
        return OperationalMemoryIntent(query_type="session_summary", scope="session")
    if any(k in normalized for k in ("ultimo arquivo deletado", "último arquivo deletado", "foi deletado hoje", "apagado hoje", "você apagou", "voce apagou")):
        return OperationalMemoryIntent(query_type="last_deleted", scope="deleted", extension=ext, time_window="today" if "hoje" in normalized else "recent")
    if any(k in normalized for k in ("ainda existe", "foi apagado", "foi deletado")):
        return OperationalMemoryIntent(query_type="file_exists", scope="deleted", extension=ext, target_name=raw)
    if any(k in normalized for k in ("o que tinha naquela pasta", "conteudo da pasta", "conteúdo da pasta", "naquela pasta", "nessa pasta")):
        return OperationalMemoryIntent(query_type="folder_snapshot", scope="folder")
    if any(k in normalized for k in ("ultima pasta", "última pasta", "qual pasta você acessou", "qual pasta voce acessou")):
        return OperationalMemoryIntent(query_type="last_folder", scope="folder")
    if any(k in normalized for k in ("ultimo print", "último print", "screenshot")):
        return OperationalMemoryIntent(query_type="last_by_extension", scope="file", extension=".png")
    if any(k in normalized for k in ("ultimo arquivo", "último arquivo", "onde voce salvou", "onde você salvou", "caminho exato")):
        return OperationalMemoryIntent(query_type="last_file", scope="file", extension=ext)
    if any(k in normalized for k in ("ultimo .", "último .", "ultimo", "último")) and ext:
        return OperationalMemoryIntent(query_type="last_by_extension", scope="file", extension=ext)
    if any(k in normalized for k in ("criou hoje", "criados hoje", "o que voce criou hoje", "o que você criou hoje")):
        return OperationalMemoryIntent(query_type="created_today", scope="file", time_window="today", extension=ext)
    if any(k in normalized for k in ("agora ha pouco", "agora há pouco", "recentemente", "ultimas acoes", "últimas ações", "ultima coisa que voce fez", "última coisa que você fez")):
        return OperationalMemoryIntent(query_type="recent_activity", scope="activity")
    return None


def _fmt_file(item: dict[str, Any]) -> str:
    return f"`{item.get('name')}` em `{item.get('path')}`"


def _extract_name_candidate(text: str) -> str:
    # heuristica leve para "esse arquivo X"
    normalized = str(text or "").strip()
    normalized = re.sub(r"[?.,;:!]+$", "", normalized)
    for marker in ("arquivo", "file"):
        idx = normalized.lower().find(marker)
        if idx >= 0:
            tail = normalized[idx + len(marker) :].strip(" :,-")
            if tail:
                normalized = tail
                break
    lowered = normalized.lower()
    for stopper in (" ainda existe", " existe", " foi apagado", " foi deletado", " foi excluido", "?"):
        pos = lowered.find(stopper)
        if pos > 0:
            normalized = normalized[:pos].strip()
            lowered = normalized.lower()
    return normalized


def answer_operational_memory_query(
    text: str,
    *,
    store: LocalMemoryStore | None = None,
) -> str | None:
    intent = detect_operational_memory_intent(text)
    if intent is None:
        return None
    logger.info("OP_MEMORY_QUERY_DETECTED query=%r", text)
    logger.info("OP_MEMORY_QUERY_TYPE type=%s scope=%s ext=%s", intent.query_type, intent.scope, intent.extension)
    mem = store or get_local_memory_store()

    if intent.query_type in {"last_file", "last_by_extension"}:
        item = mem.get_last_active_file(extension=intent.extension or None)
        if not item:
            logger.info("OP_MEMORY_QUERY_RESULT_EMPTY type=%s", intent.query_type)
            return "Nao encontrei um arquivo recente com esse criterio na memoria local."
        logger.info("OP_MEMORY_QUERY_RESULT_FOUND type=%s path=%s", intent.query_type, item.get("path"))
        return f"O ultimo arquivo que encontrei foi {_fmt_file(item)}."

    if intent.query_type == "created_today":
        files = mem.list_active_files(extension=intent.extension or None, limit=25)
        today = [f for f in files if _is_today(float(f.get("created_at", 0.0) or 0.0))]
        if not today:
            logger.info("OP_MEMORY_QUERY_RESULT_EMPTY type=created_today")
            return "Hoje eu nao tenho arquivos criados registrados na memoria local com esse filtro."
        names = ", ".join(f"`{f.get('name')}`" for f in today[:6])
        return f"Hoje eu criei {len(today)} arquivo(s). Os mais recentes foram: {names}."

    if intent.query_type == "last_folder":
        folders = mem.list_active_folders(limit=1)
        if not folders:
            return "Nao encontrei pasta recente registrada na memoria local."
        folder = folders[0]
        return f"A ultima pasta que usei foi `{folder.get('path')}`."

    if intent.query_type == "folder_snapshot":
        current = mem.get_current_session_context()
        recent_folders = list(current.get("recent_folders") or [])
        if not recent_folders:
            return "Nao encontrei pasta recente para consultar snapshot."
        snap = mem.get_folder_snapshot(recent_folders[0])
        if not snap:
            return "Nao encontrei snapshot salvo para essa pasta recente."
        children = list(snap.get("children") or [])
        listed = ", ".join(f"`{c.get('name')}`" for c in children[:8]) if children else "nenhum item"
        ts = float(snap.get("timestamp") or 0.0)
        when = datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else "desconhecido"
        return f"No ultimo snapshot da pasta `{snap.get('folder_path')}` ({when}), havia: {listed}."

    if intent.query_type == "last_deleted":
        deleted = mem.list_deleted_items(extension=intent.extension or None, limit=30)
        if intent.time_window == "today":
            deleted = [d for d in deleted if _is_today(float(d.get("deleted_at", 0.0) or 0.0))]
        if not deleted:
            return "Nao encontrei itens deletados na memoria local com esse criterio."
        item = deleted[0]
        return f"O ultimo item deletado foi `{item.get('name')}`. Caminho antigo: `{item.get('path')}`."

    if intent.query_type == "file_exists":
        name_hint = _extract_name_candidate(text).lower()
        if name_hint:
            active = mem.search_active_files(name_hint, extension=intent.extension or None, limit=1)
            if active:
                return f"Esse arquivo ainda existe: `{active[0].get('path')}`."
        deleted = mem.list_deleted_items(extension=intent.extension or None, limit=40)
        if name_hint:
            deleted = [d for d in deleted if name_hint in str(d.get("name", "")).lower() or name_hint in str(d.get("path", "")).lower()]
        if deleted:
            item = deleted[0]
            return f"Esse arquivo foi apagado. O caminho antigo era `{item.get('path')}`."
        return "Nao consegui confirmar esse arquivo na memoria local (nem como ativo nem como deletado)."

    if intent.query_type == "session_summary":
        summary = mem.get_last_session_summary()
        if not summary:
            return "Ainda nao tenho resumo de sessao salvo na memoria local."
        actions = list(summary.get("recent_actions") or [])
        files = list(summary.get("recent_files") or [])
        return (
            f"Na ultima sessao eu registrei {len(actions)} acoes recentes e toquei em {len(files)} arquivo(s). "
            f"Status final: `{summary.get('status', 'desconhecido')}`."
        )

    if intent.query_type == "recent_activity":
        actions = mem.list_recent_actions(limit=5)
        if not actions:
            return "Nao encontrei atividade recente registrada na memoria local."
        parts: list[str] = []
        now = time.time()
        for a in actions[:5]:
            act = str(a.get("action") or "acao")
            fp = str(a.get("file_path") or "")
            folder = str(a.get("folder_path") or "")
            age = max(0, int(now - float(a.get("timestamp", 0.0) or 0.0)))
            target = fp or folder or "sem alvo"
            parts.append(f"{act} ({target}, ha {age}s)")
        return "As ultimas acoes registradas foram: " + "; ".join(parts) + "."

    return None
