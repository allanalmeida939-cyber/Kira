from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata


BLOCK_FLAGS = {
    "handled": True,
    "block_gemini": True,
    "block_manus": True,
    "block_browser": True,
    "block_pc_operator": True,
    "block_proactive": True,
}


@dataclass(frozen=True)
class LocalCommandIntent:
    kind: str
    action: str
    raw_text: str
    normalized_text: str
    target: str = ""
    value: int | None = None
    source: str = "local_command"
    data: dict[str, object] = field(default_factory=dict)
    handled: bool = True
    block_gemini: bool = True
    block_manus: bool = True
    block_browser: bool = True
    block_pc_operator: bool = True
    block_proactive: bool = True

    def ack_before(self) -> str:
        if self.kind == "window":
            if self.action == "minimize_window":
                return "Beleza, vou minimizar."
            if self.action == "maximize_window":
                return "Beleza, vou maximizar."
            if self.action == "restore_window":
                return "Beleza, vou restaurar."
            if self.action == "close_window":
                return "Beleza, vou fechar."
            if self.action == "focus_window":
                return "Beleza, vou trazer para frente."
            return "Beleza, vou verificar."
        if self.kind == "volume":
            return "Ta certo, chefe. Vou ajustar o volume."
        if self.action == "open_app":
            return "Beleza, vou abrir."
        if self.action == "close_folder":
            return "Beleza, vou fechar."
        if self.action.startswith("open_"):
            return "Beleza, vou abrir."
        if self.action in {"create_folder", "rename_item", "copy_item", "zip_folder"}:
            return "Beleza, vou fazer isso agora."
        return "Beleza, vou executar."

    def to_dict(self) -> dict[str, object]:
        payload = {
            "kind": self.kind,
            "action": self.action,
            "target": self.target,
            "value": self.value,
            "source": self.source,
            "data": dict(self.data),
        }
        payload.update(BLOCK_FLAGS)
        return payload


def normalize_local_command_text(text: str | None) -> str:
    value = str(text or "").strip().lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = re.sub(r"[^\w\s.%/-]", " ", value)
    value = re.sub(r"\bparte\b", "pasta", value)
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"^(?:ei\s+|hey\s+|ok\s+)?(?:kira\s+ai|kira|akira|kiri)\s+", "", value).strip(" .!?")


def _blocked_non_local(text: str) -> bool:
    return bool(
        re.search(r"\b(cria|criar|fazer|faz|gera|gerar|monte|monta)\b.*\b(pdf|slide|slides|planilha|texto|documento)\b", text)
        or re.search(r"\b(whatsapp|zap|mensagem no whatsapp|manda mensagem)\b", text)
        or re.search(r"\b(site|pagina web|formulario|google|pesquisa|pesquisar|busca na internet|entra no)\b", text)
        or re.search(r"\b(mouse|teclado|clica|clicar|digita|arrasta)\b", text)
        or re.search(r"\b(visao|vision|camera|print|screenshot|musica|toca|spotify)\b", text)
    )


def _clean_target(value: str) -> str:
    target = str(value or "").strip()
    target = re.sub(r"^(?:o|a|os|as|uma|um)\s+", "", target)
    target = re.sub(r"^(?:janela|app|aplicativo|programa|pasta)\s+(?:(?:do|da|de)\b\s*)?", "", target)
    target = re.sub(r"\s+(?:para frente|pra frente|na frente|agora)$", "", target)
    return target.strip(" .!?")


def _detect_volume(text: str, raw_text: str) -> LocalCommandIntent | None:
    del text, raw_text
    return None
    if not re.search(r"\b(volume|som|muta|mutar|mute|mudo|desmuta|silencia|aumenta|diminui|abaixa|sobe)\b", text):
        return None
    if re.search(r"\b(desmuta|desmutar|tira do mudo|ativa o som|unmute)\b", text):
        return LocalCommandIntent("volume", "unmute_volume", raw_text, text)
    if re.search(r"\b(muta|mutar|mute|mudo|silencia|silenciar)\b", text):
        return LocalCommandIntent("volume", "mute_volume", raw_text, text)
    match = re.search(r"\b(\d{1,3})\s*(?:%|por cento)?\b", text)
    if match and re.search(r"\b(volume|som|coloca|coloque|deixa|bota|poe)\b", text):
        value = max(0, min(100, int(match.group(1))))
        return LocalCommandIntent("volume", "set_volume_absolute", raw_text, text, value=value)
    if re.search(r"\b(aumenta|aumentar|sobe|subir|mais alto)\b", text):
        return LocalCommandIntent("volume", "adjust_volume_relative", raw_text, text, value=10, data={"direction": "up"})
    if re.search(r"\b(diminui|diminuir|abaixa|baixar|baixa|mais baixo|reduz)\b", text):
        return LocalCommandIntent("volume", "adjust_volume_relative", raw_text, text, value=-10, data={"direction": "down"})
    return None


def _detect_folder_open(text: str, raw_text: str) -> LocalCommandIntent | None:
    if not re.search(r"\b(abre|abrir|abra|mostra|mostrar|mostre)\b", text):
        return None
    folder_targets = (
        (r"\b(pasta do projeto|projeto|kira v8|logs|backups)\b", "project:" + text),
        (r"\b(kira)\b.*\b(documentos|documents)\b|\b(documentos|documents)\b.*\bkira\b", "documents_kira"),
        (r"\b(kira)\b.*\b(area de trabalho|desktop)\b|\b(area de trabalho|desktop)\b.*\bkira\b", "desktop_kira"),
        (r"\b(downloads?)\b", "downloads"),
        (r"\b(documentos|documents)\b", "documents"),
        (r"\b(area de trabalho|desktop)\b", "desktop"),
    )
    for pattern, target in folder_targets:
        if re.search(pattern, text):
            return LocalCommandIntent("file", "open_folder", raw_text, text, target=target)
    if re.search(r"\b(ultima|ultimo)\b.*\b(arquivo|txt|texto)\b", text) and "pdf" not in text:
        return LocalCommandIntent("file", "open_last_file", raw_text, text, target="last_file")
    return None


def _detect_file_search(text: str, raw_text: str) -> LocalCommandIntent | None:
    if re.search(r"\b(ultima|ultimo)\b.*\b(pdf)\b", text):
        return None
    match = re.search(r"\b(?:procura|buscar|busca|abre|abrir|abra)\s+(?:o\s+)?(?:arquivo\s+)?(.+)$", text)
    if not match:
        return None
    query = match.group(1).strip()
    if not query or query in {"documentos", "downloads", "desktop", "area de trabalho"}:
        return None
    if re.search(r"\b(site|google|internet|web|whatsapp)\b", query):
        return None
    if "." not in query and not re.search(r"\b(arquivo|pdf|txt|texto)\b", text):
        return None
    return LocalCommandIntent("file", "open_file_search", raw_text, text, target=query)


def _detect_app_open(text: str, raw_text: str) -> LocalCommandIntent | None:
    if not re.search(r"\b(abre|abrir|abra|inicia|starta)\b", text):
        return None
    aliases = {
        "opera": "opera",
        "opera gx": "opera",
        "chrome": "chrome",
        "vs code": "vscode",
        "visual studio code": "vscode",
        "vscode": "vscode",
        "bloco de notas": "notepad",
        "notepad": "notepad",
        "calculadora": "calculator",
        "navegador": "browser",
        "explorador de arquivos": "explorer",
        "explorador": "explorer",
        "browser": "browser",
    }
    for alias, app in aliases.items():
        if re.search(rf"\b{re.escape(alias)}\b", text):
            return LocalCommandIntent("app", "open_app", raw_text, text, target=app)
    match = re.search(r"\b(?:abre|abrir|abra)\s+(?:o\s+)?app\s+(.+)$", text)
    if match:
        return LocalCommandIntent("app", "open_app", raw_text, text, target=match.group(1).strip())
    return None


def _detect_window_command(text: str, raw_text: str) -> LocalCommandIntent | None:
    window_terms = r"\b(janela|foco|minimiza|minimizar|maximiza|maximizar|restaura|restaurar|fecha|fechar|traz|frente|abertas|aberto)\b"
    if not re.search(window_terms, text):
        return None
    if re.search(r"\b(qual|que)\s+janela\b.*\bfoco\b|\bjanela\b.*\bfoco\b", text):
        return LocalCommandIntent("window", "active_window", raw_text, text, target="current")
    if re.search(r"\b(quais|que|o que)\b.*\b(janelas|aberto|abertas)\b", text):
        return LocalCommandIntent("window", "list_windows", raw_text, text)
    if re.search(r"\b(navegador|chrome|opera|vs code|visual studio code|vscode|bloco de notas|notepad|calculadora|explorador|pasta|documentos|downloads|kira v8)\b.*\baberto\b", text):
        target = _clean_target(re.sub(r"\b(esta|ta|aberto|aberta|tem alguma|alguma|o|a)\b", " ", text))
        return LocalCommandIntent("window", "check_window", raw_text, text, target=target)
    action = ""
    if re.search(r"\b(minimiza|minimizar)\b", text):
        action = "minimize_window"
    elif re.search(r"\b(maximiza|maximizar)\b", text):
        action = "maximize_window"
    elif re.search(r"\b(restaura|restaurar)\b", text):
        action = "restore_window"
    elif re.search(r"\b(fecha|fechar)\b", text):
        action = "close_window"
    elif re.search(r"\b(traz|coloca)\b.*\b(frente|foco)\b", text):
        action = "focus_window"
    if not action:
        return None
    if re.search(r"\b(essa|esta|atual)\s+janela\b|\bjanela\s+atual\b", text):
        return LocalCommandIntent("window", action, raw_text, text, target="current")
    match = re.search(
        r"\b(?:minimiza|minimizar|maximiza|maximizar|restaura|restaurar|fecha|fechar|traz|coloca)\s+(.+?)(?:\s+(?:para|pra|na)\s+frente|\s+em\s+foco)?$",
        text,
    )
    target = _clean_target(match.group(1) if match else "")
    if target:
        return LocalCommandIntent("window", action, raw_text, text, target=target)
    return None


def _detect_file_operation(text: str, raw_text: str) -> LocalCommandIntent | None:
    match = re.search(r"\bcria\s+(?:uma\s+)?pasta(?:\s+chamada)?\s+(.+?)(?:\s+(?:em|na|no)\s+(documentos|downloads|desktop|area de trabalho))?$", text)
    if match:
        return LocalCommandIntent("file", "create_folder", raw_text, text, target=match.group(1).strip(), data={"base": match.group(2) or ""})
    match = re.search(r"\brenomeia\s+(?:o\s+|a\s+)?(?:arquivo|pasta|item)?\s*(.+?)\s+para\s+(.+)$", text)
    if match:
        return LocalCommandIntent("file", "rename_item", raw_text, text, target=match.group(1).strip(), data={"new_name": match.group(2).strip()})
    match = re.search(r"\bfecha\s+(?:a\s+)?pasta\s+(.+)$|\bfecha\s+(documentos|downloads|area de trabalho|desktop|kira v8)$", text)
    if match:
        return LocalCommandIntent("file", "close_folder", raw_text, text, target=_clean_target(match.group(1) or match.group(2) or ""))
    if re.search(r"\bfecha\s+(?:essa|esta)\s+pasta\b", text):
        return LocalCommandIntent("file", "close_folder", raw_text, text, target="current")
    match = re.search(r"\bcopia\s+(?:o\s+|a\s+)?(?:arquivo|pasta|item)?\s*(.+?)\s+para\s+(.+)$", text)
    if match:
        return LocalCommandIntent("file", "copy_item", raw_text, text, target=match.group(1).strip(), data={"destination": match.group(2).strip()})
    match = re.search(r"\b(?:zipa|compacta|comprime)\s+(?:a\s+|o\s+)?(?:pasta\s+)?(.+)$", text)
    if match:
        return LocalCommandIntent("file", "zip_folder", raw_text, text, target=match.group(1).strip())
    return None


def detect_local_command(text: str | None) -> LocalCommandIntent | None:
    raw_text = str(text or "").strip()
    normalized = normalize_local_command_text(raw_text)
    if not normalized or _blocked_non_local(normalized):
        return None
    for detector in (_detect_volume, _detect_file_operation, _detect_window_command, _detect_folder_open, _detect_app_open, _detect_file_search):
        intent = detector(normalized, raw_text)
        if intent is not None:
            return intent
    return None


__all__ = ["BLOCK_FLAGS", "LocalCommandIntent", "detect_local_command", "normalize_local_command_text"]
