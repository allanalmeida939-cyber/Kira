from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger("kira.mem0_cloud_provider")

KEY_ALIASES = ("MEM0_API_KEY", "MEMO_API_KEY", "MEMZERO_API_KEY", "MEMOZERO_API_KEY")
DEFAULT_USER_ID = "alan"
DEFAULT_AGENT_ID = "kira"
DEFAULT_APP_ID = "kira_v8"
DEFAULT_TOP_K = 5
ALLOWED_ROLES = {"user", "assistant", "system"}
SENSITIVE_PATTERNS = (
    re.compile(r"\b(senha|password|passcode|pin)\b", re.IGNORECASE),
    re.compile(r"\b(token|api\s*key|chave\s*api|secret|segredo|credencial)\b", re.IGNORECASE),
    re.compile(r"\bcpf\b|\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", re.IGNORECASE),
    re.compile(r"\b(cartao|cartão|card)\b", re.IGNORECASE),
    re.compile(r"\b(endereco|endereço)\b", re.IGNORECASE),
)

# CIRCUIT BREAKER p/ rate-limit (429): apos um 429 do mem0, pula as buscas por
# _MEM0_RATE_LIMIT_COOLDOWN_S segundos. Sem isso, cada turno espera ~14s o SDK
# esgotar o retry interno do 429 (a causa da lentidao "14s antes de cada turno").
# Estado em modulo -> compartilhado, pois o memory_retriever cria um provider novo
# a cada busca.
_MEM0_RATE_LIMIT_COOLDOWN_S = float(os.getenv("KIRA_MEM0_RATE_LIMIT_COOLDOWN_S", "180") or "180")
_mem0_rate_limited_until = 0.0


def mem0_is_rate_limited() -> bool:
    """True enquanto estamos no cooldown de um 429 recente."""
    return time.monotonic() < _mem0_rate_limited_until


def _mem0_trip_rate_limit() -> None:
    global _mem0_rate_limited_until
    _mem0_rate_limited_until = time.monotonic() + _MEM0_RATE_LIMIT_COOLDOWN_S


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc)
    if "429" in msg or "Too Many Requests" in msg or "rate limit" in msg.lower():
        return True
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 429


class _HttpMem0Client:
    def __init__(self, *, api_key: str, host: str = "https://api.mem0.ai") -> None:
        import httpx

        self.api_key = api_key
        self.host = host.rstrip("/")
        self.user_id = hashlib.md5(api_key.encode()).hexdigest()
        self.client = httpx.Client(
            base_url=self.host,
            headers={
                "Authorization": f"Token {api_key}",
                "Mem0-User-ID": self.user_id,
            },
            timeout=30,
            # Entre um turno e outro passam bem mais que os 5 s padrão; reconectar custa ~1 s de TLS.
            limits=httpx.Limits(keepalive_expiry=120.0),
        )
        self.org_id = ""
        self.project_id = ""
        self._validate_api_key()

    def _validate_api_key(self) -> dict[str, Any]:
        response = self.client.get("/v1/ping/")
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            self.org_id = str(data.get("org_id") or "")
            self.project_id = str(data.get("project_id") or "")
        return data

    def get_project(self, fields: list[str] | None = None) -> dict[str, Any]:
        data = self._validate_api_key()
        if fields:
            return {field: data.get(field, "") for field in fields}
        return data

    def users(self) -> dict[str, Any]:
        response = self.client.get("/v1/entities/")
        response.raise_for_status()
        return response.json()

    def add(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        payload = {"messages": messages, **{k: v for k, v in kwargs.items() if v is not None}}
        if payload.get("output_format") != "v1.1":
            payload["output_format"] = "v1.1"
        payload["version"] = "v2"
        response = self.client.post("/v1/memories/", json=payload)
        response.raise_for_status()
        return response.json()

    def search(self, query: str, version: str = "v1", **kwargs: Any) -> list[dict[str, Any]]:
        payload = {"query": query, **{k: v for k, v in kwargs.items() if v is not None}}
        response = self.client.post(f"/{version}/memories/search/", json=payload)
        response.raise_for_status()
        data = response.json()
        return data.get("results", []) if isinstance(data, dict) else data

    def get_all(self, version: str = "v1", **kwargs: Any) -> list[dict[str, Any]]:
        params = {k: v for k, v in kwargs.items() if v is not None}
        if version == "v2":
            response = self.client.post("/v2/memories/", json=params)
        else:
            response = self.client.get("/v1/memories/", params=params)
        response.raise_for_status()
        data = response.json()
        return data.get("results", []) if isinstance(data, dict) else data

    def delete(self, memory_id: str) -> dict[str, Any]:
        response = self.client.delete(f"/v1/memories/{memory_id}/")
        response.raise_for_status()
        return response.json()

    def delete_all(self, **kwargs: Any) -> dict[str, Any]:
        params = {k: v for k, v in kwargs.items() if v is not None}
        response = self.client.delete("/v1/memories/", params=params)
        response.raise_for_status()
        return response.json()


class _HttpAsyncMem0Client:
    def __init__(self, *, api_key: str, host: str = "https://api.mem0.ai") -> None:
        import httpx

        self.api_key = api_key
        self.host = host.rstrip("/")
        self.user_id = hashlib.md5(api_key.encode()).hexdigest()
        self.async_client = httpx.AsyncClient(
            base_url=self.host,
            headers={
                "Authorization": f"Token {api_key}",
                "Mem0-User-ID": self.user_id,
            },
            timeout=30,
        )

    async def add(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        payload = {"messages": messages, **{k: v for k, v in kwargs.items() if v is not None}}
        if payload.get("output_format") != "v1.1":
            payload["output_format"] = "v1.1"
        payload["version"] = "v2"
        response = await self.async_client.post("/v1/memories/", json=payload)
        response.raise_for_status()
        return response.json()


@dataclass(frozen=True)
class Mem0KeyInfo:
    value: str
    source: str
    masked: str
    alias_used: bool


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)


def _mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


def configured_user_id(default: str = DEFAULT_USER_ID) -> str:
    return str(os.getenv("KIRA_MEMORY_USER_ID", default) or default).strip() or default


def configured_agent_id(default: str = DEFAULT_AGENT_ID) -> str:
    return str(os.getenv("KIRA_MEMORY_AGENT_ID", default) or default).strip() or default


def configured_app_id(default: str = DEFAULT_APP_ID) -> str:
    return str(os.getenv("KIRA_MEMORY_APP_ID", default) or default).strip() or default


def configured_top_k(default: int = 8) -> int:
    try:
        return max(1, int(os.getenv("KIRA_MEMORY_SEARCH_TOP_K", str(default)) or default))
    except Exception:
        return default


def configured_boot_limit(default: int = 50) -> int:
    try:
        return max(1, int(os.getenv("KIRA_MEMORY_BOOT_LIMIT", str(default)) or default))
    except Exception:
        return default


def resolve_mem0_api_key() -> Mem0KeyInfo | None:
    _load_dotenv_if_available()
    for name in KEY_ALIASES:
        value = str(os.getenv(name, "") or "").strip()
        if not value:
            continue
        alias_used = name != "MEM0_API_KEY"
        if alias_used:
            logger.warning(
                "MEM0_PROVIDER_KEY_ALIAS_USED alias=%s recommended=MEM0_API_KEY",
                name,
            )
        return Mem0KeyInfo(
            value=value,
            source=name,
            masked=_mask_key(value),
            alias_used=alias_used,
        )
    return None


def should_block_cloud_memory(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return False
    try:
        from services.sensitive_vault import classify_sensitive_text, third_party_sensitive_mem0_allowed

        classification = classify_sensitive_text(clean)
        if classification.sensitive and classification.owner == "alan":
            return True
        if classification.sensitive and classification.owner == "third_party":
            return not third_party_sensitive_mem0_allowed()
    except Exception:
        pass
    return any(pattern.search(clean) for pattern in SENSITIVE_PATTERNS)


def _cloud_sensitive_policy(text: str) -> dict[str, Any]:
    clean = str(text or "").strip()
    if not clean:
        return {"blocked": False, "reason": "empty", "owner": "", "category": ""}
    try:
        from services.sensitive_vault import classify_sensitive_text, third_party_sensitive_mem0_allowed

        classification = classify_sensitive_text(clean)
        if not classification.sensitive:
            logger.info("MEM0_SAVE_ALLOWED_NON_SENSITIVE")
            return {"blocked": False, "reason": "non_sensitive", "owner": "", "category": "normal"}
        if classification.owner == "alan":
            logger.info("MEM0_SAVE_BLOCKED_PERSONAL_SENSITIVE reason=alan_sensitive")
            return {"blocked": True, "reason": "alan_sensitive", "owner": "alan", "category": classification.category}
        if classification.owner == "third_party":
            if third_party_sensitive_mem0_allowed():
                logger.info("SENSITIVE_THIRD_PARTY_POLICY action=allow_mem0")
                return {"blocked": False, "reason": "third_party_allowed", "owner": "third_party", "category": classification.category}
            logger.info("SENSITIVE_THIRD_PARTY_POLICY action=blocked_or_confirm_required")
            return {"blocked": True, "reason": "third_party_blocked", "owner": "third_party", "category": classification.category}
    except Exception:
        pass
    blocked = any(pattern.search(clean) for pattern in SENSITIVE_PATTERNS)
    if blocked:
        logger.info("MEM0_SAVE_BLOCKED_PERSONAL_SENSITIVE reason=generic_sensitive")
    else:
        logger.info("MEM0_SAVE_ALLOWED_NON_SENSITIVE")
    return {"blocked": blocked, "reason": "generic_sensitive" if blocked else "non_sensitive", "owner": "", "category": ""}


def _safe_error_message(exc: BaseException) -> str:
    message = str(exc)
    info = resolve_mem0_api_key()
    if info and info.value:
        message = message.replace(info.value, "<MEM0_API_KEY>")
    return message[:500]


class Mem0CloudProvider:
    def __init__(
        self,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        app_id: str | None = None,
    ) -> None:
        self.user_id = str(user_id or configured_user_id())
        self.agent_id = str(agent_id or configured_agent_id())
        self.app_id = str(app_id or configured_app_id())
        self._client: Any | None = None
        self._async_client: Any | None = None

    def is_configured(self) -> bool:
        return resolve_mem0_api_key() is not None

    def get_client(self) -> Any:
        if self._client is None:
            key_info = resolve_mem0_api_key()
            if not key_info:
                raise ValueError("Mem0 API Key not provided. Please provide an API Key.")
            self._client = _HttpMem0Client(api_key=key_info.value)
        return self._client

    def get_async_client(self) -> Any:
        if self._async_client is None:
            key_info = resolve_mem0_api_key()
            if not key_info:
                raise ValueError("Mem0 API Key not provided. Please provide an API Key.")
            self._async_client = _HttpAsyncMem0Client(api_key=key_info.value)
        return self._async_client

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            close = getattr(getattr(client, "client", None), "close", None)
            if callable(close):
                close()

    async def aclose(self) -> None:
        client = self._async_client
        self._async_client = None
        if client is not None:
            close = getattr(getattr(client, "async_client", None), "aclose", None)
            if callable(close):
                await close()

    def health_check(self) -> dict[str, Any]:
        key_info = resolve_mem0_api_key()
        result: dict[str, Any] = {
            "ok": False,
            "api_key_found": bool(key_info),
            "api_key_source": key_info.source if key_info else "",
            "api_key_masked": key_info.masked if key_info else "",
            "alias_used": key_info.alias_used if key_info else False,
        }
        if not key_info:
            result["reason"] = "mem0_api_key_missing"
            return result
        try:
            client = self.get_client()
            project = client.get_project(fields=["name"])
            result.update(
                {
                    "ok": True,
                    "auth_ok": True,
                    "project_keys": list(project.keys()) if isinstance(project, dict) else [],
                }
            )
        except Exception as exc:
            result.update(
                {
                    "auth_ok": False,
                    "error_type": exc.__class__.__name__,
                    "safe_message": _safe_error_message(exc),
                }
            )
        return result

    def is_available(self) -> bool:
        return bool(self.health_check().get("ok"))

    def _normalize_messages(self, messages: Any) -> tuple[list[dict[str, str]], bool]:
        if isinstance(messages, str):
            clean = messages.strip()
            if not clean:
                return [], False
            return [{"role": "user", "content": clean}], False

        if not isinstance(messages, list):
            raise TypeError("messages must be a list of dicts or a string")

        normalized: list[dict[str, str]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user").strip().lower()
            if role not in ALLOWED_ROLES:
                role = "user"
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            normalized.append({"role": role, "content": content})
        return normalized, True

    def _scope_kwargs(
        self,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        app_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, str]:
        payload: dict[str, str] = {}
        resolved_user = str(user_id or self.user_id or "").strip()
        if resolved_user:
            payload["user_id"] = resolved_user
        resolved_agent = str(agent_id or "").strip()
        if resolved_agent:
            payload["agent_id"] = resolved_agent
        resolved_app = str(app_id or "").strip()
        if resolved_app:
            payload["app_id"] = resolved_app
        resolved_run = str(run_id or "").strip()
        if resolved_run:
            payload["run_id"] = resolved_run
        return payload

    def add_messages(
        self,
        messages: list[dict[str, str]] | str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        app_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized, input_was_list = self._normalize_messages(messages)
        if not normalized:
            return {"ok": False, "saved": False, "reason": "empty_messages"}
        for item in normalized:
            policy = _cloud_sensitive_policy(item["content"])
            if bool(policy.get("blocked")):
                logger.warning("MEM0_ADD_BLOCKED_SENSITIVE messages_count=%s reason=%s", len(normalized), policy.get("reason") or "")
                return {"ok": True, "saved": False, "blocked_sensitive": True, "sensitive_reason": policy.get("reason"), "sensitive_category": policy.get("category")}

        scope = self._scope_kwargs(user_id=user_id, agent_id=agent_id, app_id=app_id, run_id=run_id)
        logger.info(
            "MEM0_ADD_REQUEST messages_count=%s user_id=%s agent_id=%s app_id=%s run_id=%s",
            len(normalized),
            scope.get("user_id", ""),
            scope.get("agent_id", ""),
            scope.get("app_id", ""),
            scope.get("run_id", ""),
        )
        try:
            response = self.get_client().add(
                normalized,
                **scope,
                metadata=dict(metadata or {}),
                output_format="v1.1",
            )
        except Exception as exc:
            logger.warning(
                "MEM0_ADD_FAILED error_type=%s safe_message=%s",
                exc.__class__.__name__,
                _safe_error_message(exc),
            )
            return {
                "ok": False,
                "saved": False,
                "error_type": exc.__class__.__name__,
                "safe_message": _safe_error_message(exc),
            }

        status = self._extract_add_status(response)
        event_id = self._extract_event_id(response)
        if status == "PENDING":
            logger.info("MEM0_ADD_PENDING event_id=%s", event_id or "")
        else:
            logger.info("MEM0_ADD_OK status=%s", status or "unknown")
        return {
            "ok": True,
            "saved": True,
            "pending": status == "PENDING",
            "status": status,
            "event_id": event_id,
            "input_was_list": input_was_list,
            "raw": response,
        }

    def add_text(self, text: str, **kwargs: Any) -> dict[str, Any]:
        clean = str(text or "").strip()
        if not clean:
            return {"ok": False, "saved": False, "reason": "empty_text"}
        return self.add_messages([{"role": "user", "content": clean}], **kwargs)

    async def add_messages_async(
        self,
        messages: list[dict[str, str]] | str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        normalized, input_was_list = self._normalize_messages(messages)
        if not normalized:
            return {"ok": False, "saved": False, "reason": "empty_messages"}
        for item in normalized:
            policy = _cloud_sensitive_policy(item["content"])
            if bool(policy.get("blocked")):
                return {"ok": True, "saved": False, "blocked_sensitive": True, "sensitive_reason": policy.get("reason"), "sensitive_category": policy.get("category")}
        scope = self._scope_kwargs(
            user_id=kwargs.pop("user_id", None),
            agent_id=kwargs.pop("agent_id", None),
            app_id=kwargs.pop("app_id", None),
            run_id=kwargs.pop("run_id", None),
        )
        try:
            response = await self.get_async_client().add(
                normalized,
                **scope,
                metadata=dict(kwargs.pop("metadata", {}) or {}),
                output_format="v1.1",
            )
        except Exception as exc:
            return {
                "ok": False,
                "saved": False,
                "error_type": exc.__class__.__name__,
                "safe_message": _safe_error_message(exc),
            }
        return {
            "ok": True,
            "saved": True,
            "pending": self._extract_add_status(response) == "PENDING",
            "status": self._extract_add_status(response),
            "event_id": self._extract_event_id(response),
            "input_was_list": input_was_list,
            "raw": response,
        }

    def search(
        self,
        query: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        app_id: str | None = None,
        run_id: str | None = None,
        top_k: int = DEFAULT_TOP_K,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        clean_query = str(query or "").strip()
        if not clean_query:
            return []
        if mem0_is_rate_limited():
            logger.info(
                "MEM0_SEARCH_SKIPPED_RATE_LIMIT remaining_s=%.0f",
                max(0.0, _mem0_rate_limited_until - time.monotonic()),
            )
            return []
        scope = self._scope_kwargs(user_id=user_id)
        resolved_user = scope.get("user_id")
        advanced_filters = filters or self._build_advanced_filters(
            user_id=resolved_user,
            agent_id=agent_id,
            app_id=app_id,
            run_id=run_id,
        )

        use_advanced = bool(filters or agent_id or app_id or run_id)
        try:
            if use_advanced:
                response = self.get_client().search(
                    clean_query,
                    version="v2",
                    filters=advanced_filters,
                    top_k=top_k,
                )
            else:
                response = self.get_client().search(clean_query, user_id=resolved_user, top_k=top_k)
            results = self.normalize_results(response)
            if use_advanced and not results and resolved_user:
                logger.info("MEM0_SEARCH_ADVANCED_EMPTY_FALLBACK user_id=%s", resolved_user)
                response = self.get_client().search(clean_query, user_id=resolved_user, top_k=top_k)
                return self.normalize_results(response)
            return results
        except Exception as exc:
            rate_limited = _is_rate_limit_error(exc)
            if rate_limited:
                _mem0_trip_rate_limit()
                logger.warning(
                    "MEM0_SEARCH_RATE_LIMITED cooldown_s=%s — pulando buscas ate esfriar",
                    int(_MEM0_RATE_LIMIT_COOLDOWN_S),
                )
            logger.warning(
                "MEM0_SEARCH_FAILED error_type=%s safe_message=%s advanced=%s",
                exc.__class__.__name__,
                _safe_error_message(exc),
                use_advanced,
            )
            # NAO tentar o fallback em 429 (evita outro retry de ~14s).
            if use_advanced and resolved_user and not rate_limited:
                try:
                    response = self.get_client().search(clean_query, user_id=resolved_user, top_k=top_k)
                    return self.normalize_results(response)
                except Exception as fallback_exc:
                    logger.warning(
                        "MEM0_SEARCH_FALLBACK_FAILED error_type=%s safe_message=%s",
                        fallback_exc.__class__.__name__,
                        _safe_error_message(fallback_exc),
                    )
            return []

    def get_all(self, *, user_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        resolved_user = str(user_id or self.user_id or "").strip()
        if not resolved_user:
            return []
        try:
            response = self.get_client().get_all(user_id=resolved_user)
            return self.normalize_results(response)[: max(0, int(limit))]
        except Exception as exc:
            logger.warning(
                "MEM0_GET_ALL_FAILED error_type=%s safe_message=%s",
                exc.__class__.__name__,
                _safe_error_message(exc),
            )
            return []

    def get_all_advanced(self, filters: dict[str, Any], *, limit: int = 50) -> list[dict[str, Any]]:
        if not filters:
            return []
        try:
            response = self.get_client().get_all(version="v2", filters=filters)
            return self.normalize_results(response)[: max(0, int(limit))]
        except Exception as exc:
            logger.warning(
                "MEM0_GET_ALL_ADVANCED_FAILED error_type=%s safe_message=%s",
                exc.__class__.__name__,
                _safe_error_message(exc),
            )
            return []

    def get_boot_memories(self, *, user_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.get_all(user_id=user_id, limit=limit)

    def delete_memory(self, memory_id: str) -> dict[str, Any]:
        clean_id = str(memory_id or "").strip()
        if not clean_id:
            return {"ok": False, "deleted": False, "reason": "empty_memory_id"}
        try:
            response = self.get_client().delete(clean_id)
            return {"ok": True, "deleted": True, "raw": response}
        except Exception as exc:
            return {
                "ok": False,
                "deleted": False,
                "error_type": exc.__class__.__name__,
                "safe_message": _safe_error_message(exc),
            }

    def delete_all_for_user(self, *, user_id: str) -> dict[str, Any]:
        resolved_user = str(user_id or "").strip()
        if not resolved_user:
            return {"ok": False, "deleted": False, "reason": "empty_user_id"}
        try:
            response = self.get_client().delete_all(user_id=resolved_user)
            return {"ok": True, "deleted": True, "raw": response}
        except Exception as exc:
            return {
                "ok": False,
                "deleted": False,
                "error_type": exc.__class__.__name__,
                "safe_message": _safe_error_message(exc),
            }

    def wait_until_searchable(
        self,
        query: str,
        *,
        user_id: str,
        agent_id: str | None = None,
        app_id: str | None = None,
        run_id: str | None = None,
        timeout_seconds: float = 20,
        interval_seconds: float = 2,
    ) -> dict[str, Any]:
        started = time.monotonic()
        attempts = 0
        last_results: list[dict[str, Any]] = []
        timeout = max(float(timeout_seconds), 0.1)
        interval = max(float(interval_seconds), 0.1)
        while True:
            attempts += 1
            last_results = self.search(
                query,
                user_id=user_id,
                agent_id=agent_id,
                app_id=app_id,
                run_id=run_id,
            )
            elapsed = time.monotonic() - started
            if last_results:
                return {
                    "found": True,
                    "attempts": attempts,
                    "elapsed": round(elapsed, 3),
                    "results_count": len(last_results),
                    "results": last_results,
                }
            if elapsed >= timeout:
                return {
                    "found": False,
                    "attempts": attempts,
                    "elapsed": round(elapsed, 3),
                    "results_count": 0,
                    "results": [],
                }
            time.sleep(interval)

    async def wait_until_searchable_async(self, query: str, **kwargs: Any) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.wait_until_searchable(query, **kwargs))

    def build_context_block(
        self,
        memories: list[dict[str, Any]],
        title: str = "CONTEXTO REAL DISPONIVEL",
    ) -> str:
        lines: list[str] = []
        for item in memories or []:
            text = ""
            if isinstance(item, dict):
                text = str(item.get("memory") or item.get("text") or "").strip()
            else:
                text = str(item or "").strip()
            if not text or should_block_cloud_memory(text):
                continue
            lines.append(f"- {text}")
        if not lines:
            return ""
        return "\n".join(
            [
                f"{title}:",
                *lines,
                "",
                "REGRAS:",
                "Use somente se for relevante para a pergunta.",
                "Nao invente memoria que nao esteja aqui.",
                "Se nao houver informacao suficiente, diga que nao encontrou essa memoria.",
            ]
        )

    def normalize_results(self, response: Any) -> list[dict[str, Any]]:
        raw_items = response.get("results") if isinstance(response, dict) else response
        if not isinstance(raw_items, list):
            logger.warning("MEM0_NORMALIZE_UNEXPECTED type=%s", type(response).__name__)
            return []
        normalized: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            memory = str(item.get("memory") or item.get("text") or item.get("content") or "").strip()
            if not memory:
                continue
            normalized.append(
                {
                    "id": item.get("id") or item.get("memory_id") or "",
                    "memory": memory,
                    "score": item.get("score"),
                    "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                    "created_at": item.get("created_at") or "",
                    "updated_at": item.get("updated_at") or "",
                    "raw": item,
                }
            )
        return normalized

    def _build_advanced_filters(
        self,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        app_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        parts: list[dict[str, str]] = []
        for key, value in (
            ("user_id", user_id),
            ("agent_id", agent_id),
            ("app_id", app_id),
            ("run_id", run_id),
        ):
            clean = str(value or "").strip()
            if clean:
                parts.append({key: clean})
        if len(parts) == 1:
            return parts[0]
        return {"AND": parts}

    def _extract_add_status(self, response: Any) -> str:
        if isinstance(response, dict):
            results = response.get("results")
            if isinstance(results, list) and results and isinstance(results[0], dict):
                return str(results[0].get("status") or "").strip()
            return str(response.get("status") or "").strip()
        return ""

    def _extract_event_id(self, response: Any) -> str:
        if isinstance(response, dict):
            results = response.get("results")
            if isinstance(results, list) and results and isinstance(results[0], dict):
                return str(results[0].get("event_id") or "").strip()
            return str(response.get("event_id") or "").strip()
        return ""
