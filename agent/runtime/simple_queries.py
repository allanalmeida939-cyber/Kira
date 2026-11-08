from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


logger = logging.getLogger(__name__)
# Palavras inteiras e contexto de computador: "pirâmides" não é "ram", "memória do Brasil" não é RAM.
SYSTEM_VITALS_GLOBAL_RE = re.compile(
    r"\b(cpu|ram|processador)\b"
    r"|\b(uso|consumo|desempenho)\s+(do|da|de)\s+(pc|computador|maquina|sistema|notebook)\b"
    r"|\bmemoria\s+(ram|livre|usada|do\s+(pc|computador|sistema))\b"
    r"|\b(quanta|uso\s+de)\s+memoria\b"
)


def _system_vitals_text_only_result(kind: str) -> dict[str, Any]:
    return {
        "handled": True,
        "source": "system_vitals",
        "kind": kind,
        "response_mode": "text_only",
        "speak": False,
        "block_gemini": True,
        "block_voice": True,
    }


def _fold_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _optional_module_enabled(module_name: str) -> bool:
    try:
        config_path = Path(__file__).resolve().parents[2] / "configs" / "kira_features.json"
        data = json.loads(config_path.read_text(encoding="utf-8"))
        module = ((data.get("optional_modules") or {}).get(module_name) or {})
        return bool(module.get("enabled", False))
    except Exception:
        return False


def _system_vitals_enabled() -> bool:
    return _optional_module_enabled("system_vitals")


def _looks_like_system_vitals_command(query: str) -> bool:
    q = _fold_text(query)
    if not q:
        logger.info("SYSTEM_VITALS_GATE_NOT_MATCHED reason=empty")
        return False
    if "status do sistema" in q:
        logger.info("SYSTEM_VITALS_GATE_NOT_MATCHED reason=session_status_phrase")
        return False
    process_pattern = re.search(
        r"\bquanto\s+(?:o|a|de\s+cpu\s+o|de\s+cpu\s+a|de\s+ram\s+o|de\s+ram\s+a|de\s+memoria\s+o|de\s+memoria\s+a)\s+.+?\s+(?:esta\s+)?(?:consumindo|usando|usa|consome)\b",
        q,
    )
    matched = bool(SYSTEM_VITALS_GLOBAL_RE.search(q)) or bool(process_pattern)
    if not matched:
        logger.info("SYSTEM_VITALS_GATE_NOT_MATCHED reason=no_keywords")
    return matched


def _extract_system_vitals_process_query(query: str) -> str | None:
    q = _fold_text(query)
    patterns = (
        r"\bquanto\s+o\s+processo\s+(.+?)\s+(?:esta\s+)?(?:consumindo|usando|usa|consome)\b",
        r"\bquanto\s+(?:o|a)\s+(.+?)\s+(?:esta\s+)?(?:consumindo|usando|usa|consome)\b",
        r"\bquanto\s+de\s+(?:cpu|ram|memoria)\s+(?:o|a)\s+(.+?)\s+(?:esta\s+)?(?:consumindo|usando|usa|consome)\b",
        r"\buso\s+de\s+(?:cpu|memoria|ram)\s+d[eoa]\s+(.+?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, q)
        if not match:
            continue
        candidate = match.group(1)
        candidate = re.split(r"\s+(?:do pc|da maquina|do computador|agora)\b", candidate, maxsplit=1)[0]
        candidate = candidate.strip()
        if candidate == "kira":
            return "python"
        if candidate and candidate not in {"pc", "sistema", "computador", "maquina"}:
            return candidate
    return None


def _api_json(method: str, path: str, payload: dict | None = None) -> dict:
    url = f"http://127.0.0.1:3000{path}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib_request.Request(url, data=data, method=method.upper(), headers=headers)
    with urllib_request.urlopen(req, timeout=10.0) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}


def _publish_system_vitals_text_only(message: str) -> None:
    notice = str(message or "").strip()
    if not notice:
        return
    try:
        _api_json(
            "POST",
            "/api/transcription/publish",
            {
                "text": notice,
                "source": "system_vitals",
                "speaker": "KIRA",
                "role": "assistant",
                "final": True,
            },
        )
    except (OSError, urllib_error.URLError, json.JSONDecodeError) as exc:
        logger.warning("SYSTEM_VITALS_TEXT_ONLY_PUBLISH_FAILED error=%s", exc)


def _handle_system_vitals_query(query: str) -> dict[str, Any] | None:
    safe_text = " ".join(str(query or "").split())[:160]
    logger.info("SYSTEM_VITALS_GATE_CHECK text=%s", safe_text)
    if not _looks_like_system_vitals_command(query):
        return None
    if not _system_vitals_enabled():
        logger.info("SYSTEM_VITALS_DISABLED_SKIPPED reason=config source=simple_query")
        logger.info("GEMINI_ROUTE_BLOCKED reason=system_vitals")
        logger.info("VOICE_ROUTE_BLOCKED reason=system_vitals_text_only")
        logger.info("SYSTEM_VITALS_COMMAND_TEXT_ONLY kind=disabled")
        logger.info("SYSTEM_VITALS_VOICE_SUPPRESSED reason=local_command")
        _publish_system_vitals_text_only("O Sistema Vital esta desativado agora nas configuracoes da Kira.")
        logger.info("SYSTEM_VITALS_COMMAND_HANDLED mode=text_only kind=disabled")
        return _system_vitals_text_only_result("disabled")

    process_query = _extract_system_vitals_process_query(query)
    if process_query:
        logger.info("SYSTEM_VITALS_GATE_MATCHED kind=process query=%s", process_query)
        logger.info("GEMINI_ROUTE_BLOCKED reason=system_vitals")
        try:
            result = _api_json("GET", f"/api/system-vitals/process?name={urllib_parse.quote(process_query)}")
        except (OSError, urllib_error.URLError, json.JSONDecodeError) as exc:
            logger.info("SYSTEM_VITALS_COMMAND_FAILED reason=process_query_failed error=%s", exc)
            reply = f"Nao consegui consultar o processo {process_query} agora."
            _publish_system_vitals_text_only(reply)
            logger.info("VOICE_ROUTE_BLOCKED reason=system_vitals_text_only")
            logger.info("SYSTEM_VITALS_COMMAND_TEXT_ONLY kind=process")
            logger.info("SYSTEM_VITALS_VOICE_SUPPRESSED reason=local_command")
            logger.info("SYSTEM_VITALS_COMMAND_HANDLED mode=text_only kind=process")
            return _system_vitals_text_only_result("process")
        if result.get("matched"):
            process_name = str(result.get("process") or process_query)
            cpu = float(result.get("cpu_percent") or 0)
            memory = float(result.get("memory_mb") or 0)
            reply = f"{process_name}: {cpu:.1f}% de CPU e {memory:.1f} MB de RAM."
        else:
            reply = f"Nao encontrei o {process_query} aberto agora."
        _publish_system_vitals_text_only(reply)
        logger.info("VOICE_ROUTE_BLOCKED reason=system_vitals_text_only")
        logger.info("SYSTEM_VITALS_COMMAND_TEXT_ONLY kind=process")
        logger.info("SYSTEM_VITALS_VOICE_SUPPRESSED reason=local_command")
        logger.info("SYSTEM_VITALS_COMMAND_HANDLED mode=text_only kind=process")
        return _system_vitals_text_only_result("process")

    logger.info("SYSTEM_VITALS_GATE_MATCHED kind=global")
    logger.info("GEMINI_ROUTE_BLOCKED reason=system_vitals")
    try:
        result = _api_json("GET", "/api/system-vitals?active=true&force=true")
        _api_json(
            "POST",
            "/api/system-vitals/temporary-session",
            {"duration_ms": 60000, "interval_ms": 5000, "source": "simple_query"},
        )
    except (OSError, urllib_error.URLError, json.JSONDecodeError) as exc:
        logger.info("SYSTEM_VITALS_COMMAND_FAILED reason=global_query_failed error=%s", exc)
        _publish_system_vitals_text_only("Nao consegui consultar o desempenho do PC agora.")
        logger.info("VOICE_ROUTE_BLOCKED reason=system_vitals_text_only")
        logger.info("SYSTEM_VITALS_COMMAND_TEXT_ONLY kind=global")
        logger.info("SYSTEM_VITALS_VOICE_SUPPRESSED reason=local_command")
        logger.info("SYSTEM_VITALS_COMMAND_HANDLED mode=text_only kind=global")
        return _system_vitals_text_only_result("global")
    cpu = float(((result.get("cpu") or {}).get("usage_percent")) or 0)
    ram = float(((result.get("ram") or {}).get("usage_percent")) or 0)
    _publish_system_vitals_text_only(f"CPU: {cpu:.1f}%. RAM: {ram:.1f}%. Vou deixar o painel aberto por 1 minuto.")
    logger.info("VOICE_ROUTE_BLOCKED reason=system_vitals_text_only")
    logger.info("SYSTEM_VITALS_COMMAND_TEXT_ONLY kind=global")
    logger.info("SYSTEM_VITALS_VOICE_SUPPRESSED reason=local_command")
    logger.info("SYSTEM_VITALS_COMMAND_HANDLED mode=text_only kind=global")
    return _system_vitals_text_only_result("global")


def _looks_like_pc_operator_command(query: str) -> bool:
    q = _fold_text(query)
    if not q:
        return False
    safe_local_prefixes = (
        "abre a pasta",
        "abre pasta",
        "abrir pasta",
        "abre o arquivo",
        "abrir arquivo",
        "aumenta volume",
        "diminui volume",
        "mostra o desempenho",
        "como esta o desempenho",
        "quanto o obs esta consumindo",
        "quanto o chrome esta consumindo",
        "quanto o opera esta consumindo",
        "quanto o python esta consumindo",
    )
    if any(q.startswith(prefix) for prefix in safe_local_prefixes):
        return False
    patterns = (
        r"\bcontrola\s+(?:o\s+)?(?:meu\s+)?(?:pc|computador|desktop|tela)\b",
        r"\bmexe\s+no\s+(?:meu\s+)?(?:mouse|teclado|pc|computador|vscode|vs\s+code|codex)\b",
        r"\busa\s+(?:o\s+)?(?:mouse|teclado)\b",
        r"\bclica(?:\s+ali|\s+aqui|\s+nesse|\s+neste|\s+no|\s+em)?\b",
        r"\b(?:digita|escreve)\s+(?:no|na|nesse|neste|dentro\s+do)\s+(?:bloco\s+de\s+notas|notepad|campo|editor|vscode|vs\s+code|codex)\b",
        r"\b(?:abre|usa)\s+(?:o\s+)?(?:bloco\s+de\s+notas|notepad|vscode|vs\s+code|codex)\s+e\s+(?:escreve|digita|cola|preenche)\b",
        r"\b(?:copia|copiar|copie)\s+(?:isso|esse|este|o\s+texto|esse\s+texto|este\s+texto)\b",
        r"\b(?:copia|copiar|copie).*\b(?:cola|colar|cole)\b",
        r"\b(?:cola|colar|cole)\s+(?:ali|aqui|no|na|nesse|neste|dentro)\b",
        r"\bseleciona\s+(?:esse|este|o)\s+texto\b",
        r"\b(?:arrasta|move)\s+(?:a\s+)?janela\b",
        r"\b(?:vai|muda|leva)\s+(?:pro|para\s+o|no)\s+monitor\s+(?:dois|2|um|1)\b",
        r"\bfaz\s+isso\s+(?:na\s+tela|no\s+pc|no\s+computador|no\s+programa|na\s+janela)\b",
    )
    return any(re.search(pattern, q) for pattern in patterns)


def _handle_pc_operator_disabled_query(query: str) -> str | None:
    safe_text = " ".join(str(query or "").split())[:160]
    if not _looks_like_pc_operator_command(query):
        return None
    logger.info("DESKTOP_OPERATOR_GATE_MATCHED disabled=true text=%s", safe_text)
    if _optional_module_enabled("pc_operator"):
        return None
    logger.info("PC_OPERATOR_DISABLED_SKIPPED reason=config source=simple_query")
    logger.info("GEMINI_ROUTE_BLOCKED reason=pc_operator_disabled")
    logger.info("PC_OPERATOR_COMMAND_HANDLED kind=disabled")
    return "O controle de PC esta desativado agora nas configuracoes da Kira."


class SimpleQueryHandler:
    @staticmethod
    def handle(query: str) -> str | dict[str, Any] | None:
        system_vitals_response = _handle_system_vitals_query(query)
        if system_vitals_response:
            return system_vitals_response
        pc_operator_response = _handle_pc_operator_disabled_query(query)
        if pc_operator_response:
            return pc_operator_response
        q = str(query or "").strip().lower()
        if re.search(r"\b(que horas|horas sao|hora agora)\b", q):
            return datetime.now().strftime("%H:%M")
        if re.search(r"\b(que dia|hoje e|data de hoje)\b", q):
            now = datetime.now()
            dias = ["segunda-feira", "terca-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sabado", "domingo"]
            return f"{dias[now.weekday()]}, {now.strftime('%d/%m/%Y')}"
        math_pattern = r"quanto\s+(?:e|é)\s+([\d\.,]+)\s*([\+\-\*/]|mais|menos|vezes|dividido)\s+([\d\.,]+)"
        m = re.search(math_pattern, q)
        if m:
            n1 = float(m.group(1).replace(",", "."))
            op = m.group(2)
            n2 = float(m.group(3).replace(",", "."))
            if op in ("+", "mais"):
                r = n1 + n2
            elif op in ("-", "menos"):
                r = n1 - n2
            elif op in ("*", "vezes"):
                r = n1 * n2
            elif op in ("/", "dividido"):
                if n2 == 0:
                    return "Nao posso dividir por zero."
                r = n1 / n2
            else:
                return None
            return str(int(r)) if r == int(r) else f"{r:.2f}"
        return None
