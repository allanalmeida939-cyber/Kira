from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from livekit.agents import ChatContext

from memory.local_memory_store import get_local_memory_store
from profile_store import ProfileStoreError, load_profile
from time_utils import now_brasilia

from .session_context import BACKGROUND_CONTEXT_PREFIX, load_session_context_snapshot

if TYPE_CHECKING:
    from mem0 import AsyncMemoryClient
else:
    AsyncMemoryClient = Any


logger = logging.getLogger(__name__)


def extract_mem0_results(response) -> list[dict]:
    if isinstance(response, dict):
        results = response.get("results", [])
        return results if isinstance(results, list) else []
    return response if isinstance(response, list) else []


def preferred_user_id() -> str:
    try:
        profile = load_profile()
    except ProfileStoreError:
        return "Chefe"

    preferred_name = str(profile.get("nome_preferido") or "").strip()
    return preferred_name or "Chefe"


async def aprender_preferencias_usuario(
    mem0: AsyncMemoryClient,
    user_id: str,
    user_msg: str,
    resposta_kira: str | None = None,
) -> int:
    if mem0 is None:
        return 0

    msg = (user_msg or "").strip().lower()
    if not msg:
        return 0

    preferencias_detectadas = []

    if "sempre" in msg:
        preferencias_detectadas.append(msg)
    if "prefiro" in msg:
        preferencias_detectadas.append(msg)
    if "usa" in msg or "use" in msg:
        preferencias_detectadas.append(msg)
    if "abre" in msg and ("chrome" in msg or "youtube" in msg):
        preferencias_detectadas.append(msg)

    if not preferencias_detectadas:
        return 0

    total_salvo = 0
    for pref in dict.fromkeys(preferencias_detectadas):
        try:
            await mem0.add(
                [{"role": "user", "content": f"Preferencia do usuario detectada: {pref}"}],
                user_id=user_id,
                output_format="v1.1",
                metadata={
                    "tipo": "preferencia_usuario",
                    "conteudo": pref,
                    "data": now_brasilia().isoformat(),
                    "resposta_kira": resposta_kira,
                },
            )
            total_salvo += 1
        except Exception:
            logger.exception("Falha ao salvar preferencia detectada no mem0.")

    return total_salvo


async def revisar_historico_e_melhorar_respostas(
    mem0: AsyncMemoryClient,
    user_id: str,
) -> int:
    if mem0 is None:
        return 0

    try:
        response = await mem0.get_all(user_id=user_id)
    except Exception:
        logger.exception("Falha ao buscar historico do mem0 para revisao.")
        return 0

    historico = extract_mem0_results(response)
    comandos_repetidos: dict[str, int] = {}
    respostas_ruins = []

    for item in historico:
        if not isinstance(item, dict):
            continue

        texto = str(
            item.get("memory")
            or item.get("content")
            or item.get("metadata", {}).get("conteudo", "")
        ).strip().lower()

        if not texto:
            continue

        comandos_repetidos[texto] = comandos_repetidos.get(texto, 0) + 1

        if "errado" in texto or "nÃ£o" in texto or "nao" in texto or "corrige" in texto:
            respostas_ruins.append(texto)

    total_padroes = 0
    for cmd, qtd in comandos_repetidos.items():
        if qtd < 3:
            continue

        try:
            await mem0.add(
                [{"role": "system", "content": f"Padrao detectado no historico: '{cmd}' apareceu {qtd} vezes."}],
                user_id=user_id,
                output_format="v1.1",
                metadata={
                    "tipo": "padrao_detectado",
                    "conteudo": cmd,
                    "vezes": qtd,
                },
            )
            total_padroes += 1
        except Exception:
            logger.exception("Falha ao salvar padrao detectado no mem0.")

    if respostas_ruins:
        try:
            await mem0.add(
                [
                    {
                        "role": "system",
                        "content": (
                            "Historico indica pedidos de correcao ou insatisfacao em respostas anteriores. "
                            "Priorizar mais precisao, confirmacoes curtas e menos repeticao."
                        ),
                    }
                ],
                user_id=user_id,
                output_format="v1.1",
                metadata={
                    "tipo": "ajuste_resposta",
                    "total_correcoes_detectadas": len(respostas_ruins),
                },
            )
        except Exception:
            logger.exception("Falha ao salvar ajuste de resposta no mem0.")

    return total_padroes


async def build_initial_chat_context(
    mem0: AsyncMemoryClient,
    *,
    user_id: str | None = None,
) -> tuple[ChatContext, str, str]:
    resolved_user_id = (user_id or preferred_user_id()).strip() or "Chefe"
    initial_ctx = ChatContext()
    memory_str = ""
    results: list[dict] = []

    if mem0 is not None:
        try:
            response = await mem0.get_all(user_id=resolved_user_id)
            results = extract_mem0_results(response)
            logging.info("Retrieved %s memories using get_all", len(results) if results else 0)
        except Exception as exc:
            logging.warning("get_all failed: %s. Trying search method...", exc)
            try:
                response = await mem0.search(
                    "informacoes preferencias contexto",
                    filters={"user_id": resolved_user_id},
                )
                results = extract_mem0_results(response)
                logging.info("Retrieved %s memories using search", len(results) if results else 0)
            except Exception as search_exc:
                logging.warning("Search also failed: %s. No memories loaded.", search_exc)
                results = []
    else:
        logging.warning("Mem0 indisponivel na inicializacao. Seguindo apenas com memoria local.")

    if results:
        memories = [
            {
                "memory": result.get("memory"),
                "updated_at": result.get("updated_at", ""),
            }
            for result in results
            if isinstance(result, dict) and result.get("memory")
        ][:20]

        if memories:
            memory_str = json.dumps(memories, ensure_ascii=False)
            logging.info("Loaded %s memories into the initial context.", len(memories))
            initial_ctx.add_message(
                role="assistant",
                content=(
                    "Aqui estao informacoes importantes sobre o usuario que voce deve "
                    f"lembrar e usar nas conversas: {memory_str}."
                ),
            )
    else:
        logging.info("No memories found for this user. Starting fresh conversation.")

    restored_messages = load_session_context_snapshot(user_id=resolved_user_id)
    if restored_messages:
        for message in restored_messages:
            initial_ctx.add_message(
                role=str(message["role"]),
                content=str(message["content"]),
            )
        logging.info(
            "Restored %s recent chat message(s) from local snapshot.",
            len(restored_messages),
        )

    last_session_summary = {}
    if os.getenv("KIRA_MEMORY_PROVIDER", "mem0").strip().lower() == "mem0":
        logging.info("LOCAL_MEMORY_DISABLED reason=mem0_primary component=memory_loader")
    else:
        local_memory = get_local_memory_store()
        last_session_summary = local_memory.get_last_session_summary()
    if last_session_summary:
        recent_files = list(last_session_summary.get("recent_files") or [])[:4]
        recent_folders = list(last_session_summary.get("recent_folders") or [])[:3]
        continuity_bits: list[str] = []
        if recent_files:
            continuity_bits.append(f"arquivos recentes: {', '.join(recent_files)}")
        if recent_folders:
            continuity_bits.append(f"pastas recentes: {', '.join(recent_folders)}")
        summary_text = str(last_session_summary.get("summary") or "").strip()
        if summary_text:
            continuity_bits.append(f"resumo: {summary_text}")
        if continuity_bits:
            initial_ctx.add_message(
                role="assistant",
                content=(
                    f"{BACKGROUND_CONTEXT_PREFIX} "
                    "Continuidade operacional local da ultima sessao: "
                    + " | ".join(continuity_bits)
                ),
            )

    initial_ctx.add_message(
        role="assistant",
        content=(
            f"{BACKGROUND_CONTEXT_PREFIX} "
            "So use contexto visual quando o modo visao estiver ativo. "
            "Sem compartilhamento, nao finja que esta vendo a tela nem use contexto visual antigo."
        ),
    )
    return initial_ctx, resolved_user_id, memory_str
