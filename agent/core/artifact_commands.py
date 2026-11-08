from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
import logging

from pathlib import Path

try:
    from services.artifact_registry import (
        get_last_completed_artifact, get_last_artifact, get_last_job, get_pending_destination_job,
        list_recent_artifacts, register_completed_artifact, resolve_artifact_path_on_demand,
        update_job_status, update_last_generated,
    )
    from services.artifact_destination_manager import detect_destination_intent, folder_for_destination_intent
    from services.artifact_saver import save_downloaded_file
    from services.local_file_actions import open_file, open_folder, reveal_in_explorer
    from services.artifact_state_reconciler import reconcile_artifact_state
except Exception:  # pragma: no cover
    get_last_completed_artifact = None
    get_last_artifact = None
    get_last_job = None
    list_recent_artifacts = None
    open_file = None
    open_folder = None
    reveal_in_explorer = None
    reconcile_artifact_state = None
    get_pending_destination_job = None
    register_completed_artifact = None
    update_job_status = None
    update_last_generated = None
    resolve_artifact_path_on_demand = None
    detect_destination_intent = None
    folder_for_destination_intent = None
    save_downloaded_file = None

logger = logging.getLogger(__name__)

@dataclass
class ArtifactCommand:
    action: str
    artifact_type: str
    raw_text: str
    normalized_text: str
    confidence: float


def normalize_artifact_stt_text(text: str) -> str:
    v = str(text or "").lower().strip()
    v = unicodedata.normalize("NFD", v)
    v = "".join(ch for ch in v if unicodedata.category(ch) != "Mn")
    v = re.sub(r"[^\w\s]", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    v = re.sub(r"^(kira|pira|chefe|cara)\s+", "", v)
    fixes = {
        "pd f": "pdf",
        "pdfi": "pdf",
        "txt": "texto",
        "arquvo": "arquivo",
        "aqruivo": "arquivo",
        "arquiv": "arquivo",
        "arqui": "arquivo",
        "pasta onde ta": "pasta onde esta",
        "cade": "cade",
    }
    for k, w in fixes.items():
        v = re.sub(rf"\b{re.escape(k)}\b", w, v)
    if re.search(r"\bcade\b", v):
        v = re.sub(r"\b(?:o\s+)?(?:aqui|aki|aq)\b", "o arquivo", v)
    artifact_context = bool(
        re.search(r"\b(?:abre|abrir|mostra|mostrar|mostre|cade|localiza|localizar|revela|revelar|encontra|encontrar)\b", v)
        and re.search(r"\b(?:pdf|arquivo|documento|texto|planilha|slides?|imagem|ultimo\s+(?:arquivo|pdf))\b", v)
    )
    part_as_folder = bool(
        artifact_context
        and re.search(r"\b(?:parte|parde|pata|past)\s+(?:do|de|onde)\b", v)
    )
    if part_as_folder:
        v = re.sub(r"\b(?:parte|parde|pata|past)\b", "pasta", v)
        logger.info("STT_FUZZY_FOLDER_SYNONYM_APPLIED original=parte normalized=pasta context=artifact")
    elif re.search(r"\bparte\b", v):
        logger.info("STT_FUZZY_FOLDER_SYNONYM_SKIPPED reason=no_artifact_context")
    normalized = re.sub(r"\s+", " ", v).strip()
    logger.info("ARTIFACT_STT_NORMALIZED original=%r normalized=%r", str(text or ""), normalized)
    return normalized


def normalize_artifact_text(text: str) -> str:
    return normalize_artifact_stt_text(text)


def _artifact_type(t: str) -> str:
    if "arquivo" in t and not any(x in t for x in ("pdf", "planilha", "excel", "slide", "apresentacao", "powerpoint", "ppt", "imagem", "foto", "banner", "codigo", "python", "html", "javascript", "csharp", "texto", "documento")):
        return "any"
    if "pdf" in t:
        return "pdf"
    if any(x in t for x in ("planilha", "xlsx", "excel")):
        return "spreadsheet"
    if any(x in t for x in ("slide", "apresentacao", "powerpoint", "ppt")):
        return "slide"
    if any(x in t for x in ("imagem", "foto", "banner")):
        return "image"
    if any(x in t for x in ("codigo", "python", "html", "javascript", "csharp")):
        return "code"
    if any(x in t for x in ("texto", "documento", "arquivo")):
        return "text"
    return "any"


def is_artifact_phrase(text: str) -> bool:
    n = normalize_artifact_text(text)
    return bool(re.search(r"\b(arquivo|pdf|documento|texto|pasta|diretorio|local|caminho|onde esta salvo)\b", n) and re.search(r"\b(abre|abrir|cade|onde|mostra|mostrar|qual|lista|criados|salvou|salvo)\b", n))


def detect_artifact_command(text: str) -> ArtifactCommand | None:
    n = normalize_artifact_text(text)
    if not n:
        return None
    if re.search(r"\bpasta\s+(?:da\s+)?kira\b", n) or (
        "kira" in n and re.search(r"\b(documentos?|downloads?|area\s+de\s+trabalho|desktop)\b", n)
    ):
        return None
    if re.search(r"\b(abre o google|abre o youtube|abre o site)\b", n):
        return None
    if re.search(r"\b(cria|gera|monta|fazer|faz um pdf)\b", n):
        return None
    if re.search(r"\boculta o sistema vital\b", n):
        return None

    art = _artifact_type(n)
    if re.search(r"\b(mostra os arquivos criados hoje|lista os ultimos arquivos|quais arquivos voce criou)\b", n):
        return ArtifactCommand("list_recent", "any", text, n, 0.95)
    location_term = r"(?:pasta|diretorio|local|caminho)"
    artifact_term = r"(?:arquivo|documento|texto|pdf|planilha|slides?|imagem)"
    if (
        re.search(
            rf"\b(?:abre|abrir|mostra|mostrar|mostre|cade|localiza|localizar|revela|revelar)\s+(?:a|o)?\s*{location_term}\s+(?:(?:do|de)\s+)?(?:ultimo\s+)?{artifact_term}\b",
            n,
        )
        or re.search(
            rf"\b(?:abre|abrir|mostra|mostrar|mostre|cade|localiza|localizar|revela|revelar)\s+(?:a\s+)?(?:{location_term}\s+)?onde\s+(?:esta|ta|fica|salvou)\s+(?:o\s+)?(?:ultimo\s+)?{artifact_term}\b",
            n,
        )
        or re.search(r"\b(abre a pasta padrao de pdfs?|abre a pasta de pdfs?|abre a pasta dos pdfs?|pasta onde esta o arquivo|abre onde salvou|abre onde voce salvou|abre a pasta onde salvou|abre a pasta onde voce salvou|abre onde esta salvo|abre onde esta o arquivo|abre o local do arquivo|abre a localizacao do arquivo|pagina onde ta o arquivo|onde esta salvo o pdf|onde esta salvo o arquivo)\b", n)
    ):
        return ArtifactCommand("open_folder", art, text, n, 0.93)
    if re.search(
        r"\b(?:abre|abrir|abra)\s+(?:o\s+|a\s+)?(?:ultimo\s+|ultima\s+)?(?:pdf|arquivo|texto|documento)(?:\s+que\s+(?:voce\s+)?(?:criou|gerou|fez))?\b",
        n,
    ):
        return ArtifactCommand("open_last_file", art, text, n, 0.92)
    if re.search(r"\b(cade o pdf|onde esta o pdf|cade o arquivo|onde esta o arquivo|cade o texto|onde esta o texto|cade o arquivo de texto|onde salvou|onde foi salvo|qual arquivo foi criado|qual foi o ultimo arquivo|mostra o caminho do arquivo|mostra o arquivo|(?:me )?mostra o ultimo arquivo)\b", n):
        return ArtifactCommand("query_last_artifact", art, text, n, 0.94)
    if is_artifact_phrase(n):
        return ArtifactCommand("show_status", art, text, n, 0.75)
    return None


def handle_pending_artifact_destination(text: str) -> dict:
    if not callable(get_pending_destination_job):
        return {"handled": False}
    job = get_pending_destination_job()
    if not isinstance(job, dict):
        return {"handled": False}
    normalized = normalize_artifact_text(text)
    intent = detect_destination_intent(text, str(job.get("task_type") or "")) if callable(detect_destination_intent) else None
    if intent is None or intent.mode == "ask_later":
        if normalized not in {"padrao", "pode salvar no padrao", "salva ai mesmo", "deixa nos pdfs"}:
            return {"handled": False}
        intent = detect_destination_intent("salva na pasta padrao", str(job.get("task_type") or ""))
    folder = folder_for_destination_intent(str(job.get("task_type") or ""), intent) if callable(folder_for_destination_intent) else None
    staged = list(job.get("staged_files") or [])
    if folder is None or not staged or not callable(save_downloaded_file):
        return {"handled": False}
    job_id = str(job.get("job_id") or "")
    kind = str(job.get("task_type") or "arquivo")
    logger.info("PENDING_ARTIFACT_DESTINATION_GATE_MATCHED job_id=%s", job_id)
    logger.info("PENDING_ARTIFACT_DESTINATION_RESOLVED mode=%s path=%s", intent.mode, folder)
    final_path = save_downloaded_file(staged[0], kind, str(job.get("title_hint") or kind), destination_folder=folder)
    if callable(register_completed_artifact):
        register_completed_artifact(job_id, kind, str(final_path), source="manus", metadata={"resolved_after_user_destination": True})
    if callable(update_job_status):
        update_job_status(job_id, "completed_saved", extra={"output_files": [str(final_path)], "staged_files": [], "resolved_destination_folder": str(folder)})
    if callable(update_last_generated):
        update_last_generated(kind, str(final_path), job_id=job_id)
    logger.info("PENDING_ARTIFACT_DESTINATION_SAVE_OK path=%s", final_path)
    logger.info("ARTIFACT_SAVED_AFTER_USER_DESTINATION job_id=%s", job_id)
    logger.info("PENDING_ARTIFACT_DESTINATION_GEMINI_BLOCKED")
    folder_label = {"pdf": "PDFs", "text": "Textos", "slides": "Slides", "prompt": "Prompts", "code": "Codigos"}.get(kind, "Arquivos")
    return {
        "handled": True,
        "response": f"Pronto, salvei {('o PDF' if kind == 'pdf' else 'o arquivo')} na pasta de {folder_label}.",
        "action": "resolve_pending_destination",
        "kind": kind,
        "path": str(final_path),
        "speak": True,
        "job_id": job_id,
    }


def handle_artifact_command(text: str) -> dict:
    pending_result = handle_pending_artifact_destination(text)
    if pending_result.get("handled"):
        return pending_result
    cmd = detect_artifact_command(text)
    if cmd is None:
        normalized = normalize_artifact_text(text)
        last_job = get_last_job() if callable(get_last_job) else None
        if isinstance(last_job, dict) and re.fullmatch(r"(deu certo|deu tudo certo|salvou|conseguiu salvar|foi salvo)", normalized):
            job_kind = str(last_job.get("task_type") or "any").lower()
            cmd = ArtifactCommand("query_last_artifact", job_kind, text, normalized, 0.9)
    if cmd is None:
        return {"handled": False}
    kind = cmd.artifact_type
    action = cmd.action
    logger.info("ARTIFACT_COMMAND_GATE_MATCHED action=%s kind=%s", action, kind)
    logger.info("ARTIFACT_COMMAND_LOOKUP_BEGIN scope=session_then_global")
    lookup_kind = None if kind in {"any", ""} else kind
    logger.info("ARTIFACT_LAST_LOOKUP_BEGIN kind=%s", lookup_kind or "any")
    artifact = get_last_completed_artifact(lookup_kind) if callable(get_last_completed_artifact) else None
    if artifact is None and callable(get_last_artifact):
        artifact = get_last_artifact(lookup_kind)
    if isinstance(artifact, dict) and (artifact.get("file_path") or artifact.get("path")):
        logger.info("ARTIFACT_LAST_LOOKUP_FOUND path=%s", str(artifact.get("file_path") or artifact.get("path")))
    else:
        logger.info("ARTIFACT_LAST_LOOKUP_NONE kind=%s", lookup_kind or "any")
    job = get_last_job() if callable(get_last_job) else None
    job_status = str((job or {}).get("status") or "").strip().lower()
    path = str((artifact or {}).get("file_path") or (artifact or {}).get("path") or "").strip()
    current_job_id = str((job or {}).get("job_id") or "").strip() or None
    if callable(reconcile_artifact_state) and current_job_id:
        try:
            state = reconcile_artifact_state(
                job_id=current_job_id,
                kind=lookup_kind,
            )
            if state.saved:
                path = state.final_path
                job_status = "completed"
        except Exception:
            logger.warning("ARTIFACT_STATE_RECONCILE_FAILED_IGNORED")
    recovered_path = False
    missing_registered_path = False
    if path and action in {"open_folder", "open_last_file", "query_last_artifact", "show_status"}:
        original_path = path
        if callable(resolve_artifact_path_on_demand):
            resolved_path, recovered_path = resolve_artifact_path_on_demand(artifact, lookup_kind)
            path = str(resolved_path or "")
        else:
            exists = Path(path).exists()
            logger.info("ARTIFACT_PATH_CHECK path=%s exists=%s", path, str(exists).lower())
            if not exists:
                path = ""
        # Verificacao final de disco: exists + is_file + size > 0 antes de confirmar/abrir.
        if path:
            verify_target = Path(path)
            logger.info("ARTIFACT_DISK_VERIFY_BEGIN path=%s", path)
            try:
                disk_ok = verify_target.exists() and verify_target.is_file() and verify_target.stat().st_size > 0
            except Exception:
                disk_ok = False
            if disk_ok:
                logger.info("ARTIFACT_DISK_VERIFY_OK path=%s size=%s", path, verify_target.stat().st_size)
            else:
                logger.info("ARTIFACT_DISK_VERIFY_FAILED path=%s", path)
                path = ""
        missing_registered_path = bool(original_path and not path)
        if missing_registered_path:
            logger.info("ARTIFACT_OPEN_BLOCKED_MISSING_PATH path=%s", original_path)
    label = "arquivo" if kind in {"", "any"} else kind.upper()
    pending_statuses = {"queued", "submitted", "sending_to_manus", "sent_to_manus", "worker_queued", "running", "pending_result", "waiting_executor", "manus_failed", "completed_waiting_destination", "completed_no_file"}
    if path:
        logger.info("ARTIFACT_COMMAND_LAST_ARTIFACT_FOUND path=%s", path)

    def _job_response() -> str | None:
        if not isinstance(job, dict) or job_status not in pending_statuses:
            return None
        task_id = str(job.get("manus_task_id") or job.get("task_id") or "").strip()
        task_url = str(job.get("manus_task_url") or job.get("task_url") or "").strip()
        del task_url
        if job_status == "manus_failed":
            logger.info("ARTIFACT_COMMAND_JOB_FOUND status=%s task_id_present=%s", job_status, str(bool(task_id)).lower())
            return "Interface: o Manus não conseguiu concluir a tarefa."
        if job_status == "completed_waiting_destination":
            return f"Interface: {label} pronto. Onde voce quer que eu salve?"
        if job_status == "completed_no_file":
            return "Interface: o Manus terminou, mas nao devolveu um arquivo baixavel."
        logger.info("ARTIFACT_COMMAND_LAST_JOB_PENDING job_id=%s status=%s task_id_present=%s", str(job.get("job_id") or "none"), job_status, str(bool(task_id)).lower())
        del task_id
        return "Interface: a tarefa foi enviada ao Manus e ainda está aguardando conclusão."

    if action == "list_recent":
        items = list_recent_artifacts(limit=10, kind=lookup_kind) if callable(list_recent_artifacts) else []
        paths = [str(item.get("file_path") or item.get("path") or "").strip() for item in items]
        paths = [item for item in paths if item]
        if paths:
            response = "Interface: arquivos recentes:\n" + "\n".join(paths[:5])
        else:
            logger.info("ARTIFACT_COMMAND_NO_ARTIFACT_FOUND kind=%s", kind)
            response = "Interface: ainda nao existe arquivo criado nesta sessao."
    elif action in {"open_folder"}:
        logger.info("ARTIFACT_COMMAND_OPEN_FOLDER_BEGIN")
        logger.info("ARTIFACT_COMMAND_BLOCKED_OPEN_WEB reason=artifact_command_priority")
        if path:
            logger.info("ARTIFACT_OPEN_FOLDER_BEGIN folder=%s", Path(path).parent)
            open_result = open_folder(Path(path)) if callable(open_folder) else {"ok": False}
            if not isinstance(open_result, dict) or not bool(open_result.get("ok")):
                logger.warning("ARTIFACT_COMMAND_OPEN_FOLDER_FAILED path=%s result=%s", path, open_result)
                response = "Interface: nao consegui abrir a pasta do PDF."
            else:
                logger.info("ARTIFACT_COMMAND_OPEN_FOLDER_OK folder=%s", Path(path).parent)
                logger.info("ARTIFACT_OPEN_FOLDER_OK folder=%s", Path(path).parent)
                response = (
                    "Interface: encontrei o PDF na pasta padrao, atualizei o registro e abri a pasta."
                    if recovered_path and kind == "pdf"
                    else f"Interface: abrindo a pasta do {('PDF' if kind == 'pdf' else 'arquivo')}."
                )
        else:
            response = None if missing_registered_path else _job_response()
            if response is None:
                response = (
                    f"Interface: nao encontrei esse {('PDF' if kind == 'pdf' else 'arquivo')} no caminho salvo nem na pasta padrao."
                    if missing_registered_path
                    else (
                        "Interface: ainda nao existe PDF criado nesta sessao."
                        if kind == "pdf"
                        else "Interface: ainda nao existe arquivo criado nesta sessao."
                    )
                )
    elif action in {"open_last_file"}:
        if path:
            logger.info("ARTIFACT_OPEN_FILE_BEGIN path=%s", path)
            open_result = open_file(Path(path)) if callable(open_file) else {"ok": False}
            if not isinstance(open_result, dict) or not bool(open_result.get("ok")):
                response = "Interface: nao consegui abrir esse arquivo."
            else:
                logger.info("ARTIFACT_OPEN_FILE_OK path=%s", path)
                response = (
                    "Interface: encontrei o PDF na pasta padrao, atualizei o registro e abri o arquivo."
                    if recovered_path and kind == "pdf"
                    else f"Interface: abrindo o {('PDF' if kind == 'pdf' else 'arquivo')}."
                )
        else:
            response = None if missing_registered_path else _job_response()
            if response is None:
                response = (
                    f"Interface: nao encontrei esse {('PDF' if kind == 'pdf' else 'arquivo')} no caminho salvo nem na pasta padrao."
                    if missing_registered_path
                    else f"Interface: ainda nao existe {label} criado nesta sessao."
                )
    else:
        if path:
            response = f"Interface: arquivo criado em {path}"
        else:
            response = None if missing_registered_path else _job_response()
            if response is None:
                response = (
                    f"Interface: nao encontrei esse {('PDF' if kind == 'pdf' else 'arquivo')} no caminho salvo nem na pasta padrao."
                    if missing_registered_path
                    else f"Interface: ainda nao existe {label} criado nesta sessao."
                )
    logger.info("ARTIFACT_COMMAND_HANDLED_TEXT_ONLY action=%s kind=%s path_present=%s", action, kind, str(bool(path)).lower())
    logger.info("ARTIFACT_COMMAND_TEXT_ONLY_NOTICE")
    logger.info("ARTIFACT_COMMAND_GEMINI_DISABLED")
    logger.info("ARTIFACT_COMMAND_VOICE_DISABLED")
    logger.info("ARTIFACT_GEMINI_DISABLED")
    logger.info("ARTIFACT_VOICE_DISABLED")
    logger.info("ARTIFACT_COMMAND_EARLY_RETURN")
    logger.info("LOCAL_COMMAND_HANDLED_EARLY_RETURN source=artifact_command")
    logger.info("NORMAL_REPLY_BLOCKED_BY_LOCAL_COMMAND source=artifact_command")
    logger.info("GHOST_REPLY_BLOCKED_BY_LOCAL_COMMAND source=artifact_command")
    logger.info("LOCAL_COMMAND_NO_VOICE_REQUEST_CREATED")
    return {"handled": True, "response": response, "action": action, "kind": kind, "path": path}
