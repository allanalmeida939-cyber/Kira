from __future__ import annotations

import json
import time
import unicodedata
from pathlib import Path
from typing import Any
from uuid import uuid4


BASE_DIR = Path(__file__).resolve().parents[1]
CONFIGS_DIR = BASE_DIR / "configs"
LOGS_DIR = BASE_DIR / "logs"
WHATSAPP_CONTACTS_PATH = CONFIGS_DIR / "whatsapp_contacts.json"
WHATSAPP_SENDS_PATH = LOGS_DIR / "whatsapp_sends.jsonl"


def normalize_whatsapp_value(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def fold_whatsapp_value(value: object) -> str:
    normalized = normalize_whatsapp_value(value)
    decomposed = unicodedata.normalize("NFKD", normalized.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _extract_digits(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _default_contacts_payload() -> dict[str, Any]:
    return {
        "version": "",
        "updated_at": "",
        "contacts": [],
    }


def load_whatsapp_contacts() -> dict[str, Any]:
    _ensure_parent(WHATSAPP_CONTACTS_PATH)
    if not WHATSAPP_CONTACTS_PATH.exists():
        payload = _default_contacts_payload()
        save_whatsapp_contacts(payload.get("contacts") or [])
        return payload
    try:
        raw = json.loads(WHATSAPP_CONTACTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        payload = _default_contacts_payload()
        save_whatsapp_contacts(payload.get("contacts") or [])
        return payload
    contacts = raw.get("contacts") if isinstance(raw, dict) else []
    return {
        "version": str(raw.get("version") or "") if isinstance(raw, dict) else "",
        "updated_at": str(raw.get("updated_at") or "") if isinstance(raw, dict) else "",
        "contacts": contacts if isinstance(contacts, list) else [],
    }


def save_whatsapp_contacts(contacts: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "version": str(int(time.time() * 1000)),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "contacts": contacts,
    }
    _ensure_parent(WHATSAPP_CONTACTS_PATH)
    WHATSAPP_CONTACTS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def sanitize_contact_record(record: dict[str, Any]) -> dict[str, Any]:
    whatsapp_name = normalize_whatsapp_value(
        record.get("whatsapp_name") or record.get("name")
    )
    name = normalize_whatsapp_value(record.get("name") or whatsapp_name)
    aliases_raw = record.get("aliases") if isinstance(record.get("aliases"), list) else []
    aliases: list[str] = []
    seen_aliases: set[str] = set()
    for item in aliases_raw:
        alias = normalize_whatsapp_value(item)
        folded = fold_whatsapp_value(alias)
        if not alias or not folded or folded in seen_aliases:
            continue
        seen_aliases.add(folded)
        aliases.append(alias)

    phone = _extract_digits(record.get("phone") or record.get("number"))
    number = _extract_digits(record.get("number") or phone)
    relationship = normalize_whatsapp_value(record.get("relationship") or "outro") or "outro"
    priority = normalize_whatsapp_value(record.get("priority") or "normal") or "normal"
    contact_id = normalize_whatsapp_value(record.get("id")) or f"wa-{uuid4().hex[:12]}"

    return {
        "id": contact_id,
        "name": name,
        "aliases": aliases,
        "number": number,
        "phone": phone or number,
        "whatsapp_name": whatsapp_name or name,
        "relationship": relationship,
        "priority": priority,
    }


def upsert_whatsapp_contact(record: dict[str, Any]) -> dict[str, Any]:
    sanitized = sanitize_contact_record(record)
    payload = load_whatsapp_contacts()
    contacts = [sanitize_contact_record(item) for item in payload.get("contacts") or []]
    sanitized_number = sanitized.get("number") or ""
    sanitized_name_folded = fold_whatsapp_value(sanitized.get("name"))
    kept: list[dict[str, Any]] = []
    replaced = False
    for item in contacts:
        same_id = str(item.get("id") or "") == str(sanitized.get("id") or "")
        same_number = bool(sanitized_number) and str(item.get("number") or "") == sanitized_number
        same_name = fold_whatsapp_value(item.get("name")) == sanitized_name_folded
        if same_id or same_number or same_name:
            if not replaced:
                kept.append(sanitized)
                replaced = True
            continue
        kept.append(item)
    if not replaced:
        kept.append(sanitized)
    save_whatsapp_contacts(kept)
    return sanitized


def delete_whatsapp_contact(contact_id: object) -> bool:
    """Remove o contato pelo id (aba WhatsApp). False se não existir."""
    wanted = normalize_whatsapp_value(contact_id)
    payload = load_whatsapp_contacts()
    contacts = [sanitize_contact_record(item) for item in payload.get("contacts") or []]
    kept = [item for item in contacts if item.get("id") != wanted]
    if not wanted or len(kept) == len(contacts):
        return False
    save_whatsapp_contacts(kept)
    return True


def _contact_terms(contact: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    name = normalize_whatsapp_value(contact.get("name"))
    if name:
        terms.append(name)
    whatsapp_name = normalize_whatsapp_value(contact.get("whatsapp_name"))
    if whatsapp_name:
        terms.append(whatsapp_name)
    for alias in contact.get("aliases") or []:
        alias_text = normalize_whatsapp_value(alias)
        if alias_text:
            terms.append(alias_text)
    return terms


def resolve_whatsapp_contact(query: str, *, contacts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    normalized_query = normalize_whatsapp_value(query)
    folded_query = fold_whatsapp_value(normalized_query)
    if not folded_query:
        return {
            "found": False,
            "query": normalized_query,
            "matches": [],
            "contact": None,
            "reason": "empty_query",
        }

    pool = contacts
    if pool is None:
        payload = load_whatsapp_contacts()
        pool = payload.get("contacts") or []

    exact_matches: list[tuple[int, dict[str, Any], str]] = []
    partial_matches: list[tuple[int, dict[str, Any], str]] = []
    for raw_contact in pool:
        contact = sanitize_contact_record(raw_contact)
        for term in _contact_terms(contact):
            folded_term = fold_whatsapp_value(term)
            if not folded_term:
                continue
            score = len(folded_term)
            if folded_term == folded_query:
                exact_matches.append((score, contact, term))
            elif folded_query in folded_term or folded_term in folded_query:
                partial_matches.append((score, contact, term))

    ranked = sorted(exact_matches or partial_matches, key=lambda item: item[0], reverse=True)
    matches = []
    seen_ids: set[str] = set()
    for _, contact, matched_term in ranked:
        contact_id = str(contact.get("id") or "")
        if not contact_id or contact_id in seen_ids:
            continue
        seen_ids.add(contact_id)
        matches.append(
            {
                "id": contact_id,
                "name": contact.get("name"),
                "aliases": list(contact.get("aliases") or []),
                "number": contact.get("number"),
                "phone": contact.get("phone"),
                "whatsapp_name": contact.get("whatsapp_name"),
                "relationship": contact.get("relationship"),
                "priority": contact.get("priority"),
                "matched_term": matched_term,
            }
        )

    if not matches:
        return {
            "found": False,
            "query": normalized_query,
            "matches": [],
            "contact": None,
            "reason": "not_found",
        }

    chosen = matches[0]
    return {
        "found": True,
        "query": normalized_query,
        "matches": matches,
        "contact": chosen,
        "ambiguous": len(matches) > 1 and matches[0]["matched_term"] == matches[1]["matched_term"],
        "reason": "ok",
    }


def append_whatsapp_send_record(record: dict[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    payload.setdefault("request_id", f"wa-send-{uuid4().hex[:12]}")
    payload.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    _ensure_parent(WHATSAPP_SENDS_PATH)
    with WHATSAPP_SENDS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


def load_whatsapp_send_records(*, limit: int = 100) -> list[dict[str, Any]]:
    if not WHATSAPP_SENDS_PATH.exists():
        return []
    try:
        lines = WHATSAPP_SENDS_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-max(int(limit), 1):]:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records
