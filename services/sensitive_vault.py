from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger("kira.app")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "secure" / "kira_sensitive_vault.sqlite"
DEFAULT_KEY_PATH = PROJECT_ROOT / "data" / "secure" / ".kira_sensitive_vault.key"
OWNER_USER_ID = "alan"

SENSITIVE_KEYWORDS = (
    "senha",
    "password",
    "token",
    "api key",
    "chave api",
    "chave de api",
    "secret",
    "segredo",
    "cpf",
    "rg",
    "documento",
    "endereco",
    "endereço",
    "cep",
    "email",
    "e-mail",
    "conta",
    "login",
    "credencial",
    "cartao",
    "cartão",
    "banco",
    "pix",
    "telefone",
)

OWNER_PATTERNS = (
    re.compile(r"\b(meu|minha|meus|minhas)\b", re.IGNORECASE),
    re.compile(r"\bdo\s+alan\b|\bde\s+alan\b", re.IGNORECASE),
)
THIRD_PARTY_PATTERNS = (
    re.compile(r"\b(?:de|do|da)\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÀ-ÿ-]{2,})"),
    re.compile(r"\b(?:o|a)\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÀ-ÿ-]{2,})\b"),
)


@dataclass(frozen=True)
class SensitiveClassification:
    sensitive: bool
    owner: str
    category: str
    label: str
    masked_preview: str
    third_party_name: str = ""


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize(text: str | None) -> str:
    return " ".join(str(text or "").strip().split())


def _lower(text: str | None) -> str:
    return _normalize(text).lower()


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def third_party_sensitive_mem0_allowed() -> bool:
    return _env_flag("KIRA_ALLOW_THIRD_PARTY_SENSITIVE_MEM0", default=False)


def reveal_requires_confirmation() -> bool:
    return _env_flag("KIRA_SENSITIVE_REVEAL_REQUIRES_CONFIRMATION", default=True)


def _category_from_text(text: str) -> str:
    lower = _lower(text)
    if "cpf" in lower or re.search(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", lower):
        return "cpf"
    if any(term in lower for term in ("senha", "password", "passcode", "pin")):
        return "password"
    if any(term in lower for term in ("token", "api key", "chave api", "chave de api", "secret", "segredo")):
        return "token"
    if any(term in lower for term in ("endereco", "endereço", "cep", "rua", "avenida")):
        return "address"
    if any(term in lower for term in ("email", "e-mail")):
        return "email"
    if any(term in lower for term in ("cartao", "cartão", "banco", "pix", "conta")):
        return "financial"
    if "telefone" in lower:
        return "phone"
    if any(term in lower for term in ("rg", "documento")):
        return "document"
    return "secret"


def _label_from_text(text: str, category: str) -> str:
    clean = _normalize(text)
    lower = clean.lower()
    if category == "password":
        match = re.search(r"\bsenha\s+(?:do|da|de)\s+([^:=é]+)", lower)
        if match:
            return f"senha {match.group(1).strip()}"[:80]
        return "senha"
    if category == "cpf":
        return "CPF"
    if category == "token":
        return "token/API key"
    if category == "address":
        return "endereço"
    if category == "email":
        return "e-mail"
    if category == "financial":
        return "dado financeiro"
    if category == "phone":
        return "telefone pessoal"
    if category == "document":
        return "documento"
    return clean[:80] or category


def _extract_value(text: str) -> str:
    clean = _normalize(text)
    patterns = (
        r"(?:[:=]|\b(?:é|eh|e)\b)\s*(.+)$",
        r"\b(?:vale|como)\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, clean, flags=re.IGNORECASE)
        if match:
            value = match.group(1).strip(" .")
            if value:
                return value
    category = _category_from_text(clean)
    if category in {"password", "token", "cpf"}:
        parts = clean.strip(" .").split()
        if parts:
            return parts[-1].strip(" .")
    return clean


def mask_secret(value: str | None, *, category: str = "secret") -> str:
    clean = _normalize(value)
    if not clean:
        return ""
    if category == "cpf":
        digits = re.sub(r"\D", "", clean)
        if len(digits) >= 4:
            return f"***.***.***-{digits[-2:]}"
        return "***DOCUMENTO***"
    if category in {"password", "token"}:
        tail = clean[-4:] if len(clean) > 4 else ""
        return ("*" * max(8, min(len(clean), 16))) + tail
    if category == "email" and "@" in clean:
        name, domain = clean.split("@", 1)
        return f"{name[:1]}***@{domain}"
    return clean[:2] + "***" if len(clean) > 2 else "***"


def safe_text_preview(text: str | None) -> str:
    classification = classify_sensitive_text(text)
    if not classification.sensitive:
        return _normalize(text)[:180]
    return classification.masked_preview


def classify_sensitive_text(text: str | None) -> SensitiveClassification:
    clean = _normalize(text)
    lower = clean.lower()
    category = _category_from_text(clean)
    sensitive = any(term in lower for term in SENSITIVE_KEYWORDS)
    sensitive = sensitive or bool(re.search(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", clean))
    if not sensitive:
        return SensitiveClassification(False, "", "normal", "", "")

    third_party = ""
    for pattern in THIRD_PARTY_PATTERNS:
        match = pattern.search(clean)
        if match:
            candidate = str(match.group(1) or "").strip()
            if candidate and candidate.lower() not in {"Chefe", "CPF".lower()}:
                third_party = candidate
                break
    owner = "third_party" if third_party and not any(p.search(clean) for p in OWNER_PATTERNS) else OWNER_USER_ID
    value = _extract_value(clean)
    label = _label_from_text(clean, category)
    masked = mask_secret(value, category=category)
    return SensitiveClassification(True, owner, category, label, masked, third_party)


def is_personal_sensitive(text: str | None) -> bool:
    classification = classify_sensitive_text(text)
    return classification.sensitive and classification.owner == OWNER_USER_ID


def is_third_party_sensitive(text: str | None) -> bool:
    classification = classify_sensitive_text(text)
    return classification.sensitive and classification.owner == "third_party"


class _CryptoBackend:
    name = "none"

    def encrypt(self, data: bytes) -> bytes:
        raise NotImplementedError

    def decrypt(self, data: bytes) -> bytes:
        raise NotImplementedError


class _DpapiCrypto(_CryptoBackend):
    name = "windows_dpapi"

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("dpapi_unavailable")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

    def encrypt(self, data: bytes) -> bytes:
        buffer = ctypes.create_string_buffer(data)
        in_blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        out_blob = DATA_BLOB()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            self._kernel32.LocalFree(out_blob.pbData)

    def decrypt(self, data: bytes) -> bytes:
        buffer = ctypes.create_string_buffer(data)
        in_blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        out_blob = DATA_BLOB()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            self._kernel32.LocalFree(out_blob.pbData)


class _FernetCrypto(_CryptoBackend):
    name = "fernet_local_key"

    def __init__(self, key_path: Path = DEFAULT_KEY_PATH) -> None:
        from cryptography.fernet import Fernet

        self._fernet_cls = Fernet
        self._key_path = key_path
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            key = key_path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            key_path.write_bytes(key)
            try:
                os.chmod(key_path, 0o600)
            except Exception:
                pass
        self._fernet = Fernet(key)

    def encrypt(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data)

    def decrypt(self, data: bytes) -> bytes:
        return self._fernet.decrypt(data)


def _select_crypto() -> _CryptoBackend:
    try:
        backend = _DpapiCrypto()
        secrets.token_bytes(1)
        return backend
    except Exception:
        try:
            return _FernetCrypto()
        except Exception as exc:
            logger.error("SENSITIVE_VAULT_ERROR error_type=%s", exc.__class__.__name__)
            raise RuntimeError("sensitive_vault_crypto_unavailable") from exc


class SensitiveVault:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH, *, owner_user_id: str = OWNER_USER_ID) -> None:
        self.db_path = Path(db_path)
        self.owner_user_id = owner_user_id
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.crypto = _select_crypto()
        self.init_db()
        logger.info("SENSITIVE_VAULT_INIT_OK backend=%s", self.crypto.name)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sensitive_items (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    label TEXT NOT NULL,
                    encrypted_value BLOB NOT NULL,
                    encrypted_context BLOB,
                    masked_preview TEXT,
                    source TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    last_accessed_at TEXT,
                    is_active INTEGER DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sensitive_audit (
                    id TEXT PRIMARY KEY,
                    item_id TEXT,
                    action TEXT,
                    owner_user_id TEXT,
                    category TEXT,
                    label TEXT,
                    created_at TEXT,
                    safe_details TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sensitive_items_lookup ON sensitive_items(owner_user_id, category, label, is_active)")

    def _encrypt_json(self, payload: Any) -> bytes:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self.crypto.encrypt(raw)

    def _decrypt_json(self, payload: bytes | None) -> Any:
        if not payload:
            return None
        return json.loads(self.crypto.decrypt(bytes(payload)).decode("utf-8"))

    def _audit(self, conn: sqlite3.Connection, *, item_id: str | None, action: str, category: str, label: str, safe_details: dict[str, Any] | None = None) -> None:
        conn.execute(
            """
            INSERT INTO sensitive_audit(id, item_id, action, owner_user_id, category, label, created_at, safe_details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                item_id,
                action,
                self.owner_user_id,
                category,
                label,
                _now_iso(),
                json.dumps(dict(safe_details or {}), ensure_ascii=False, sort_keys=True),
            ),
        )

    def save(
        self,
        *,
        raw_value: str,
        category: str,
        label: str,
        context: str = "",
        source: str = "memory_command",
    ) -> dict[str, Any]:
        clean_value = _normalize(raw_value)
        clean_label = _normalize(label) or category
        clean_category = _normalize(category) or "secret"
        masked = mask_secret(clean_value, category=clean_category)
        item_id = str(uuid.uuid4())
        now = _now_iso()
        logger.info("SENSITIVE_VAULT_SAVE_BEGIN category=%s label=%s", clean_category, clean_label)
        encrypted_value = self._encrypt_json({"value": clean_value})
        encrypted_context = self._encrypt_json({"context": _normalize(context)}) if context else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sensitive_items(
                    id, owner_user_id, category, label, encrypted_value, encrypted_context,
                    masked_preview, source, created_at, updated_at, last_accessed_at, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    item_id,
                    self.owner_user_id,
                    clean_category,
                    clean_label,
                    encrypted_value,
                    encrypted_context,
                    masked,
                    _normalize(source),
                    now,
                    now,
                    None,
                ),
            )
            self._audit(conn, item_id=item_id, action="save", category=clean_category, label=clean_label, safe_details={"masked_preview": masked})
        logger.info("SENSITIVE_VAULT_SAVE_OK item_id=%s", item_id)
        return {"ok": True, "item_id": item_id, "category": clean_category, "label": clean_label, "masked_preview": masked}

    def find(self, *, query: str = "", category: str | None = None, label: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        clean_query = _normalize(query)
        clean_category = _normalize(category) if category else ""
        clean_label = _normalize(label) if label else ""
        logger.info("SENSITIVE_VAULT_QUERY_BEGIN label=%s", clean_label or clean_query or clean_category)
        where = ["owner_user_id = ?", "is_active = 1"]
        params: list[Any] = [self.owner_user_id]
        if clean_category:
            where.append("category = ?")
            params.append(clean_category)
        if clean_label:
            where.append("label LIKE ?")
            params.append(f"%{clean_label}%")
        elif clean_query:
            where.append("(label LIKE ? OR category LIKE ? OR masked_preview LIKE ?)")
            params.extend([f"%{clean_query}%", f"%{clean_query}%", f"%{clean_query}%"])
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, owner_user_id, category, label, masked_preview, source, created_at, updated_at, last_accessed_at, is_active
                FROM sensitive_items
                WHERE {' AND '.join(where)}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        results = [dict(row) for row in rows]
        if results:
            logger.info("SENSITIVE_VAULT_QUERY_FOUND masked_preview=%s", results[0].get("masked_preview") or "")
        return results

    def reveal(self, *, item_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sensitive_items WHERE id = ? AND owner_user_id = ? AND is_active = 1",
                (item_id, self.owner_user_id),
            ).fetchone()
            if row is None:
                return {"ok": False, "reason": "not_found"}
            payload = self._decrypt_json(row["encrypted_value"])
            now = _now_iso()
            conn.execute("UPDATE sensitive_items SET last_accessed_at = ? WHERE id = ?", (now, item_id))
            self._audit(
                conn,
                item_id=item_id,
                action="reveal",
                category=str(row["category"]),
                label=str(row["label"]),
                safe_details={"masked_preview": str(row["masked_preview"] or "")},
            )
        logger.info("SENSITIVE_VAULT_REVEAL_OK item_id=%s", item_id)
        return {
            "ok": True,
            "item_id": item_id,
            "category": str(row["category"]),
            "label": str(row["label"]),
            "masked_preview": str(row["masked_preview"] or ""),
            "value": str((payload or {}).get("value") or ""),
        }

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM sensitive_items WHERE owner_user_id = ? AND is_active = 1", (self.owner_user_id,)).fetchone()[0])
            audit_count = int(conn.execute("SELECT COUNT(*) FROM sensitive_audit").fetchone()[0])
        return {
            "active": True,
            "path_exists": self.db_path.exists(),
            "items_count": count,
            "audit_count": audit_count,
            "crypto_backend": self.crypto.name,
            "db_path": str(self.db_path),
        }

    def raw_database_contains(self, needle: str) -> bool:
        if not self.db_path.exists():
            return False
        raw = self.db_path.read_bytes()
        return str(needle).encode("utf-8") in raw


def get_sensitive_vault(db_path: Path | str = DEFAULT_DB_PATH) -> SensitiveVault:
    return SensitiveVault(db_path=db_path)


def get_sensitive_vault_status() -> dict[str, Any]:
    try:
        return get_sensitive_vault().status()
    except Exception as exc:
        logger.warning("SENSITIVE_VAULT_ERROR error_type=%s", exc.__class__.__name__)
        return {"active": False, "path_exists": DEFAULT_DB_PATH.exists(), "items_count": 0, "audit_count": 0, "error_type": exc.__class__.__name__}


def content_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:12]
