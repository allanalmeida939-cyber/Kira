"""Saúde pessoal: só dados que o usuário importou depois de consentir, guardados cifrados e apagáveis a qualquer momento.

A Kira nunca diagnostica, nunca inventa valor e nunca trata ausência de registro como ausência de problema.
Desligado por padrão (KIRA_HEALTH_ENABLED=0).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from services.secure_store import SecureStore

UNITS = {
    "peso": {"kg"},
    "pressao_sistolica": {"mmHg"},
    "pressao_diastolica": {"mmHg"},
    "glicose": {"mg/dL"},
    "frequencia_cardiaca": {"bpm"},
    "passos": {"passos"},
    "sono": {"h"},
}
MAX_RECORDS = 2000
MIN_TREND_POINTS = 3
LIMITATIONS = "Informação organizada a partir dos seus registros; não substitui avaliação profissional."
CONSENT_SCOPE = "Ler e guardar, cifrados neste computador, apenas os registros de saúde que você importar."


def enabled() -> bool:
    from services.kira_settings import flag_enabled

    return flag_enabled("KIRA_HEALTH_ENABLED")


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def validate_record(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("registro inválido")
    category = str(raw.get("category") or "").strip().lower()
    unit = str(raw.get("unit") or "").strip()
    if category not in UNITS:
        raise ValueError(f"categoria não suportada: {category or 'vazia'}")
    if unit not in UNITS[category]:
        raise ValueError(f"unidade {unit or 'vazia'} não serve para {category}")
    value = _number(raw.get("value"))
    if value is None or value < 0:
        raise ValueError("valor ausente ou negativo")
    recorded = str(raw.get("recordedAt") or "")
    try:
        datetime.fromisoformat(recorded.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("data do registro inválida") from exc
    reference = raw.get("referenceRange") if isinstance(raw.get("referenceRange"), dict) else None
    low, high = (_number(reference.get("low")), _number(reference.get("high"))) if reference else (None, None)
    record = {
        "category": category, "value": value, "unit": unit, "recordedAt": recorded,
        "source": " ".join(str(raw.get("source") or "arquivo importado").split())[:60],
        "referenceRange": {"low": low, "high": high} if low is not None or high is not None else None,
    }
    record["recordId"] = "h" + hashlib.sha1(f"{category}|{recorded}|{value}|{unit}".encode()).hexdigest()[:12]
    return record


def summarize(records: list[dict[str, Any]], category: str) -> dict[str, Any]:
    chosen = sorted((r for r in records if r["category"] == category), key=lambda r: r["recordedAt"])
    if not chosen:
        return {"categoria": category, "registros": 0, "observacao": "Não há registros dessa categoria; isso não indica nada sobre a sua saúde."}
    units = {r["unit"] for r in chosen}
    if len(units) != 1:
        return {"categoria": category, "registros": len(chosen), "observacao": "Unidades diferentes nos registros; não comparo valores."}
    latest = chosen[-1]
    summary: dict[str, Any] = {
        "categoria": category, "registros": len(chosen), "unidade": latest["unit"],
        "ultimo": {"valor": latest["value"], "data": latest["recordedAt"][:10], "origem": latest["source"]},
        "limitacoes": LIMITATIONS,
    }
    reference = latest.get("referenceRange")
    if reference:
        low, high = reference.get("low"), reference.get("high")
        outside = (low is not None and latest["value"] < low) or (high is not None and latest["value"] > high)
        summary["faixa_de_referencia"] = {"min": low, "max": high, "ultimo_fora_da_faixa": outside}
    if len(chosen) >= MIN_TREND_POINTS:
        half = len(chosen) // 2
        first = sum(r["value"] for r in chosen[:half]) / half
        second = sum(r["value"] for r in chosen[half:]) / (len(chosen) - half)
        change = (second - first) / first * 100 if first else 0.0
        summary["tendencia"] = "estável" if abs(change) < 3 else ("subindo" if change > 0 else "descendo")
    else:
        summary["tendencia"] = f"poucos registros (menos de {MIN_TREND_POINTS}) para falar em tendência"
    return summary


class HealthVault:
    def __init__(self, store: SecureStore | None = None) -> None:
        self._store = store or SecureStore()

    def consent(self) -> dict[str, Any] | None:
        return self._store.load("health_consent")

    def give_consent(self) -> dict[str, Any]:
        value = {"scope": CONSENT_SCOPE, "grantedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self._store.save("health_consent", value)
        return value

    def records(self) -> list[dict[str, Any]]:
        return list((self._store.load("health_records") or {}).get("records") or [])

    def import_records(self, rows: list[object]) -> dict[str, Any]:
        if not self.consent():
            raise PermissionError("sem consentimento")
        if not isinstance(rows, list) or not rows:
            raise ValueError("arquivo sem registros")
        accepted: dict[str, dict[str, Any]] = {r["recordId"]: r for r in self.records()}
        rejected = []
        for index, raw in enumerate(rows[:MAX_RECORDS]):
            try:
                record = validate_record(raw)
            except ValueError as exc:
                rejected.append({"linha": index + 1, "motivo": str(exc)})
                continue
            accepted[record["recordId"]] = record
        self._store.save("health_records", {"records": list(accepted.values())[-MAX_RECORDS:]})
        return {"importados": len(rows) - len(rejected), "rejeitados": rejected[:20]}

    def revoke(self) -> bool:
        removed_records = self._store.delete("health_records")
        removed_consent = self._store.delete("health_consent")
        return removed_records or removed_consent


__all__ = ["CONSENT_SCOPE", "HealthVault", "LIMITATIONS", "UNITS", "enabled", "summarize", "validate_record"]
