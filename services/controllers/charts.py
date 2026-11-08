"""Gráficos validados antes de renderizar: unidades coerentes, valores reais (lacuna fica lacuna) e fonte preservada.

Nada aqui completa série nem estima valor. Rosca/pizza só quando as partes somam 100%.
"""

from __future__ import annotations

import math
from typing import Any

CHART_TYPES = ("line", "bar", "donut")
POINT_KINDS = ("observed", "forecast", "estimate")
MAX_POINTS = 40
MAX_SERIES = 6


class ChartError(ValueError):
    """Mensagem amigável: por que o gráfico não pode ser mostrado."""


def _value(raw: object) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ChartError("valor que não é número")
    return float(raw)


def validate_chart(model: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(model, dict) or model.get("chartType") not in CHART_TYPES:
        raise ChartError("tipo de gráfico não suportado")
    series = model.get("series")
    if not isinstance(series, list) or not 1 <= len(series) <= MAX_SERIES:
        raise ChartError("gráfico sem séries")
    labels = [str(label)[:24] for label in (model.get("xAxis") or {}).get("labels") or []]
    if not 1 <= len(labels) <= MAX_POINTS:
        raise ChartError("eixo sem rótulos")
    units = {str(s.get("unit") or "") for s in series if isinstance(s, dict)}
    if len(units) != 1:
        raise ChartError("unidades diferentes no mesmo gráfico")
    unit = units.pop()
    clean_series = []
    for raw in series:
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, list) or len(data) != len(labels):
            raise ChartError("série com tamanho diferente do eixo")
        points = []
        for point in data:
            entry = point if isinstance(point, dict) else {"value": point}
            kind = entry.get("kind") or "observed"
            if kind not in POINT_KINDS:
                raise ChartError("tipo de ponto inválido")
            points.append({"value": _value(entry.get("value")), "kind": kind})
        if all(p["value"] is None for p in points):
            raise ChartError("série sem nenhum valor")
        clean_series.append({"name": str(raw.get("name") or "")[:60], "unit": unit, "data": points})
    if model["chartType"] == "donut":
        values = [p["value"] for p in clean_series[0]["data"]]
        if len(clean_series) != 1 or unit != "%" or any(v is None or v < 0 for v in values) or abs(sum(values) - 100.0) > 0.5:
            raise ChartError("as partes não somam 100%; mostro como barras")
    return {
        "chartId": str(model.get("chartId") or "grafico")[:40],
        "chartType": model["chartType"],
        "title": str(model.get("title") or "")[:120],
        "xAxis": {"label": str((model.get("xAxis") or {}).get("label") or "")[:40], "labels": labels},
        "yAxis": {"label": str((model.get("yAxis") or {}).get("label") or "")[:40], "unit": unit},
        "series": clean_series,
        "sourceIds": [str(s)[:40] for s in model.get("sourceIds") or []][:6],
        "updatedAt": str(model.get("updatedAt") or "")[:40] or None,
    }


__all__ = ["ChartError", "validate_chart"]
