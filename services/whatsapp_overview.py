"""Resumo do WhatsApp para a aba: conta, números de hoje, conversas recentes e contatos da Kira.

Só quem e quando: o texto das mensagens não sai daqui, e os números (da conta e dos contatos) vão mascarados.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

RECENT_LIMIT = 6
CONTACTS_LIMIT = 60


def mask_phone(value: object) -> str:
    """5585912345678 -> "+55 85 9•••• ••78" (código do país e DDD, o resto escondido)."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 8:
        return ""
    if digits.startswith("55") and len(digits) >= 12:
        return f"+55 {digits[2:4]} {digits[4]}•••• ••{digits[-2:]}"
    return f"+{digits[:2]} •••• ••{digits[-2:]}"


def account(me: object) -> dict[str, str]:
    """Nome do perfil quando ele diz algo; perfil "." ou vazio vira o número mascarado ("Conectado como ." antes)."""
    data = me if isinstance(me, dict) else {}
    name = " ".join(str(data.get("name") or "").split())[:40]
    label = name if re.search(r"\w", name) else mask_phone(data.get("phone"))
    return {"name": name, "label": label}


def _moment(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone() if moment.tzinfo else moment.astimezone()


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def build_overview(status: dict[str, Any], messages: object, contacts: object, *, mode: str, now: datetime) -> dict[str, Any]:
    today = now.astimezone().date()
    received = sent = 0
    chats_today: set[str] = set()
    by_contact: dict[str, dict[str, Any]] = {}
    for item in messages if isinstance(messages, list) else []:
        if not isinstance(item, dict):
            continue
        moment = _moment(item.get("timestamp"))
        if moment is None:
            continue
        key = str(item.get("jid") or item.get("contact") or "")
        from_me = bool(item.get("fromMe"))
        is_today = moment.date() == today
        if is_today:
            chats_today.add(key)
            sent += from_me
            received += not from_me
        entry = by_contact.setdefault(key, {"contact": _clean(item.get("contact"), 40) or "Contato", "received_today": 0, "last": moment, "from_me": from_me})
        if moment >= entry["last"]:
            entry["last"], entry["from_me"] = moment, from_me
        if is_today and not from_me:
            entry["received_today"] += 1
    recent = sorted(by_contact.values(), key=lambda entry: entry["last"], reverse=True)[:RECENT_LIMIT]
    people = [
        {"id": _clean(contact.get("id"), 40), "name": _clean(contact.get("name"), 30), "number": mask_phone(contact.get("number") or contact.get("phone")), "whatsapp_name": _clean(contact.get("whatsapp_name"), 30)}
        for contact in (contacts if isinstance(contacts, list) else [])
        if isinstance(contact, dict) and contact.get("name")
    ]
    people.sort(key=lambda person: person["name"].lower())
    return {
        "connected": bool((status or {}).get("connected")),
        "account": account((status or {}).get("me")),
        "mode": str(mode or "assistant"),
        "today": {"received": received, "sent": sent, "chats": len(chats_today)},
        "recent": [
            {
                "contact": entry["contact"],
                "received_today": entry["received_today"],
                "last": entry["last"].strftime("%H:%M") if entry["last"].date() == today else entry["last"].strftime("%d/%m"),
                "from_me": entry["from_me"],
            }
            for entry in recent
        ],
        "contacts": {"count": len(people), "items": people[:CONTACTS_LIMIT]},
    }


__all__ = ["account", "build_overview", "mask_phone"]
