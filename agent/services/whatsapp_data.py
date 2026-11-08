from __future__ import annotations

import importlib.util
from pathlib import Path


_ROOT_MODULE_PATH = Path(__file__).resolve().parents[2] / "services" / "whatsapp_data.py"
_SPEC = importlib.util.spec_from_file_location(
    "kira_root_services_whatsapp_data",
    _ROOT_MODULE_PATH,
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover
    raise ImportError(f"Nao foi possivel carregar {_ROOT_MODULE_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

BASE_DIR = _MODULE.BASE_DIR
CONFIGS_DIR = _MODULE.CONFIGS_DIR
LOGS_DIR = _MODULE.LOGS_DIR
WHATSAPP_CONTACTS_PATH = _MODULE.WHATSAPP_CONTACTS_PATH
WHATSAPP_SENDS_PATH = _MODULE.WHATSAPP_SENDS_PATH
append_whatsapp_send_record = _MODULE.append_whatsapp_send_record
fold_whatsapp_value = _MODULE.fold_whatsapp_value
load_whatsapp_contacts = _MODULE.load_whatsapp_contacts
load_whatsapp_send_records = _MODULE.load_whatsapp_send_records
normalize_whatsapp_value = _MODULE.normalize_whatsapp_value
resolve_whatsapp_contact = _MODULE.resolve_whatsapp_contact
sanitize_contact_record = _MODULE.sanitize_contact_record
save_whatsapp_contacts = _MODULE.save_whatsapp_contacts
upsert_whatsapp_contact = _MODULE.upsert_whatsapp_contact

__all__ = [
    "BASE_DIR",
    "CONFIGS_DIR",
    "LOGS_DIR",
    "WHATSAPP_CONTACTS_PATH",
    "WHATSAPP_SENDS_PATH",
    "append_whatsapp_send_record",
    "fold_whatsapp_value",
    "load_whatsapp_contacts",
    "load_whatsapp_send_records",
    "normalize_whatsapp_value",
    "resolve_whatsapp_contact",
    "sanitize_contact_record",
    "save_whatsapp_contacts",
    "upsert_whatsapp_contact",
]
