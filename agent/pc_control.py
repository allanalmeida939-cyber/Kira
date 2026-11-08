import ctypes
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from PIL import Image, ImageGrab
try:
    from agent.browser_guard import evaluate_browser_action, is_browser_home_request
except Exception:  # pragma: no cover - compatibilidade com execucao direta em agent/
    from browser_guard import evaluate_browser_action, is_browser_home_request
from navigation_intent_resolver import normalize_text, resolve_navigation_intent
from time_utils import now_brasilia
from memory.local_memory_store import get_local_memory_store
from memory.operational_command_resolution import resolve_operational_memory_target


class PCControlError(Exception):
    pass


logger = logging.getLogger(__name__)
RPC_E_CHANGED_MODE = -2147417850
WINRAR_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WinRAR"
ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}
MAX_ARCHIVE_BYTES = 2 * 1024**3


@contextmanager
def _volume_com_scope():
    comtypes_module = None
    initialized_here = False
    logger.info("VOLUME_BACKEND_COM_INIT_BEGIN")
    try:
        import comtypes as comtypes_module

        try:
            comtypes_module.CoInitialize()
            initialized_here = True
            logger.info("VOLUME_BACKEND_COM_INIT_OK")
        except Exception as exc:
            error_code = int(getattr(exc, "winerror", 0) or getattr(exc, "hresult", 0) or 0)
            if error_code in {RPC_E_CHANGED_MODE, RPC_E_CHANGED_MODE & 0xFFFFFFFF}:
                logger.info("VOLUME_BACKEND_COM_INIT_RPC_CHANGED_MODE")
            else:
                raise
        yield
    finally:
        if initialized_here and comtypes_module is not None:
            try:
                comtypes_module.CoUninitialize()
                logger.info("VOLUME_BACKEND_COM_UNINIT_OK")
            except Exception as exc:
                logger.warning("VOLUME_BACKEND_COM_UNINIT_FAILED error=%s", exc)


@dataclass
class ScreenshotExecutionResult:
    ok: bool
    captured: bool
    saved: bool
    clipboard: bool
    path: str = ""
    filename: str = ""
    file_size: int = 0
    image_width: int = 0
    image_height: int = 0
    requested_name: str = ""
    requested_dir: str = ""
    error: str = ""


class PCController:
    def __init__(self) -> None:
        self._stopwatch_started_at: float | None = None
        self._search_timeout_seconds = 3.0
        self._search_max_depth = 5
        self._organize_timeout_seconds = 8.0
        self._shortcuts_dir = Path.cwd() / "atalhos_apps"
        self._shortcuts_dir.mkdir(parents=True, exist_ok=True)
        self._foreground_window_aliases = {
            "app",
            "aplicativo",
            "essa pasta",
            "essa janela",
            "explorador",
            "explorador de arquivos",
            "explorer",
            "explorer.exe",
            "file explorer",
            "isso",
            "isto",
            "janela",
            "janela atual",
            "janela em foco",
            "pasta",
            "pasta atual",
            "pasta em foco",
        }
        self._app_aliases = {
            "chrome": "chrome",
            "google chrome": "chrome",
            "discord": "discord",
            "vscode": "code",
            "vs code": "code",
            "code": "code",
            "notepad": "notepad",
            "bloco de notas": "notepad",
            "calc": "calc",
            "calculadora": "calc",
            "explorer": "explorer",
            "explorador": "explorer",
            "spotify": "spotify",
            "edge": "msedge",
            "microsoft edge": "msedge",
        }
        self._process_aliases = {
            "chrome": "chrome.exe",
            "google chrome": "chrome.exe",
            "discord": "Discord.exe",
            "vscode": "Code.exe",
            "vs code": "Code.exe",
            "code": "Code.exe",
            "notepad": "notepad.exe",
            "bloco de notas": "notepad.exe",
            "calc": "CalculatorApp.exe",
            "calculadora": "CalculatorApp.exe",
            "spotify": "Spotify.exe",
            "edge": "msedge.exe",
            "microsoft edge": "msedge.exe",
        }
        self._web_aliases = {
            "navegador": "https://www.google.com",
            "browser": "https://www.google.com",
            "google": "https://www.google.com",
            "youtube": "https://www.youtube.com",
            "gmail": "https://mail.google.com",
            "openai": "https://www.openai.com",
            "github": "https://github.com",
            "instagram": "https://www.instagram.com",
            "whatsapp": "https://web.whatsapp.com",
            "whatsapp web": "https://web.whatsapp.com",
        }
        self._local_memory = get_local_memory_store()
        self._volume_state_lock = RLock()
        self._volume_state = {
            "current_volume_percent": 100,
            "is_muted": False,
            "last_nonzero_volume_percent": 100,
        }

    def _user_home(self) -> Path:
        return Path.home()

    def _known_locations(self) -> dict[str, Path]:
        home = self._user_home()
        desktop = Path(os.environ.get("USERPROFILE", str(home))) / "Desktop"
        downloads = home / "Downloads"
        documents = home / "Documents"
        pictures = home / "Pictures"
        music = home / "Music"
        videos = home / "Videos"
        return {
            "desktop": desktop,
            "area de trabalho": desktop,
            "downloads": downloads,
            "download": downloads,
            "documentos": documents,
            "documents": documents,
            "imagens": pictures,
            "pictures": pictures,
            "musicas": music,
            "music": music,
            "videos": videos,
            "home": home,
            "usuario": home,
            "raiz do projeto": Path.cwd(),
            "projeto": Path.cwd(),
        }

    def _normalize_text(self, value: str | None) -> str:
        stripped = (value or "").strip().strip("\"'")
        return normalize_text(stripped)

    def _get_instagram_username(self) -> str:
        profile = self._local_memory.get_user_profile() if self._local_memory else {}
        candidates = (
            "instagram_username",
            "instagram_user",
            "instagram",
            "insta_username",
            "insta_user",
            "username_instagram",
        )
        for key in candidates:
            value = profile.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _resolve_navigation(self, text: str) -> dict[str, object]:
        resolution = resolve_navigation_intent(
            text,
            instagram_username=self._get_instagram_username(),
        )
        payload = resolution.to_dict()
        logger.info('[NAV] command_received="%s"', text)
        logger.info(
            '[NAV] platform="%s" target_type="%s" normalized_target="%s" resolved_url="%s" fallback_used=%s confidence=%.2f note="%s"',
            payload.get("platform", ""),
            payload.get("target_type", ""),
            payload.get("normalized_target", ""),
            payload.get("resolved_url", ""),
            payload.get("fallback_used", False),
            float(payload.get("confidence", 0.0)),
            payload.get("note", ""),
        )
        return payload


    def _post_local_json(self, path: str, payload: dict[str, object] | None = None) -> None:
        url = f"http://127.0.0.1:3000{path}"
        body = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.5) as response:
            response.read()

    def _open_internal_music(self, query: str, source_command: str) -> str:
        logger.info('music_route_target_internal command="%s" query="%s"', source_command, query)
        try:
            self._post_local_json("/api/kira-ui/control", {"action": "open_tab", "target": "music"})
            self._post_local_json("/api/music/play", {"query": query})
            return f"Tocando no player interno da Kira: {query}."
        except Exception as exc:
            raise PCControlError(
                "Nao consegui iniciar a música no player interno da Kira agora. Verifique se a interface principal esta ativa."
            ) from exc

    def _send_music_command(self, command: str, value: Any | None = None) -> str:
        try:
            self._post_local_json("/api/kira-ui/control", {"action": "music_command", "command": command, "value": value})
        except Exception as exc:
            raise PCControlError("Nao consegui controlar o player interno da Kira agora.") from exc
        messages = {
            "play_pause": "Alternando play/pause da música na Kira.",
            "next": "Pulando para a próxima música na Kira.",
            "previous": "Voltando para a música anterior na Kira.",
            "set_volume": "Ajustando o volume da música na Kira.",
        }
        return messages.get(command, "Comando de música enviado para a Kira.")

    def _looks_like_music_request(self, text: str) -> bool:
        normalized = self._normalize_text(text)
        if not normalized:
            return False
        has_music_token = any(token in normalized for token in ("musica", "música", "song", "som", "playlist", "faixa"))
        has_play_token = any(token in normalized for token in ("toca", "tocar", "toque", "coloca", "reproduz", "reproduzir"))
        return has_music_token or has_play_token

    def _extract_music_query(self, text: str) -> str:
        normalized = self._normalize_text(text)
        query = normalized
        query = re.sub(r"^\s*(kira[,:\s]+)?", "", query).strip()
        query = re.sub(r"\b(toca|toque|tocar|coloca|reproduz|reproduzir)\b", "", query).strip()
        query = re.sub(
            r"\b(no teu navegador|no seu navegador|no navegador da kira|no navegador interno|na tua tela|na sua tela|dentro de voce|dentro da kira)\b",
            "",
            query,
        ).strip()
        query = re.sub(r"\s+", " ", query).strip(" -")
        if not query:
            return "música para trabalhar"
        return query

    def open_in_local_browser(self, url: str, source_command: str = "") -> str:
        """FASE 0 — abre no navegador LOCAL do Windows (Chrome/Edge/Firefox padrao).
        Preservado para o caso 'abre no meu navegador/Chrome' (alvo=local). O navegador
        embutido (aba Internet) e o Browser Agent externo, em caminho separado."""
        logger.info('browser_route_opening_local_browser command="%s" url="%s"', source_command, url)
        webbrowser.open(url)
        return f"Abrindo {url}."

    # compat: nome antigo aponta para a abertura no navegador local
    def _open_external_browser(self, url: str, source_command: str = "") -> str:
        return self.open_in_local_browser(url, source_command)

    def _find_program_shortcut(self, program_name: str) -> Path | None:
        wanted = self._normalize_text(program_name)
        if not self._shortcuts_dir.exists():
            return None

        allowed_suffixes = {".lnk", ".url", ".exe", ".bat", ".cmd"}
        partial_matches: list[Path] = []

        for candidate in self._shortcuts_dir.iterdir():
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in allowed_suffixes:
                continue

            stem = self._normalize_text(candidate.stem)
            filename = self._normalize_text(candidate.name)
            if stem == wanted or filename == wanted:
                return candidate
            if wanted in stem or stem in wanted:
                partial_matches.append(candidate)

        return partial_matches[0] if partial_matches else None

    def _escape_ps(self, value: str) -> str:
        return value.replace("'", "''")

    def _run_command(
        self,
        args: list[str],
        timeout: float | None = None,
        *,
        allow_explorer_termination: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if self._targets_explorer_termination(args) and not allow_explorer_termination:
            raise PCControlError(
                "Por seguranca, nao encerro explorer.exe sem um comando explicitamente destrutivo."
            )
        try:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PCControlError("A acao demorou mais do que o esperado.") from exc

    def _run_powershell(
        self,
        script: str,
        timeout: float | None = None,
        *,
        allow_explorer_termination: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if self._script_targets_explorer_termination(script) and not allow_explorer_termination:
            raise PCControlError(
                "Por seguranca, nao encerro explorer.exe sem um comando explicitamente destrutivo."
            )
        return self._run_command(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            timeout=timeout,
            allow_explorer_termination=allow_explorer_termination,
        )

    def _targets_explorer_termination(self, args: list[str]) -> bool:
        normalized_args = [str(arg or "").strip().lower() for arg in args]
        if not normalized_args:
            return False

        executable = Path(normalized_args[0]).name
        if executable not in {"taskkill", "taskkill.exe"}:
            return False

        return any(
            token in {"explorer", "explorer.exe", "windows explorer"}
            or token.endswith("\\explorer.exe")
            for token in normalized_args[1:]
        )

    def _script_targets_explorer_termination(self, script: str) -> bool:
        normalized_script = " ".join(str(script or "").strip().lower().split())
        patterns = (
            r"\bstop-process\b.*\bexplorer(?:\.exe)?\b",
            r"\btaskkill\b.*\bexplorer(?:\.exe)?\b",
        )
        return any(re.search(pattern, normalized_script) for pattern in patterns)

    def _resolve_process_name(self, program_name: str) -> str:
        cleaned = self._normalize_text(program_name)
        process_name = self._process_aliases.get(cleaned, program_name)
        if not process_name.lower().endswith(".exe"):
            process_name = f"{process_name}.exe"
        return process_name

    def _is_explorer_target(self, value: str) -> bool:
        cleaned = self._normalize_text(value)
        return cleaned in {
            "explorer",
            "explorer.exe",
            "explorador",
            "explorador de arquivos",
            "file explorer",
            "windows explorer",
        }

    def _should_close_foreground_window(self, value: str) -> bool:
        cleaned = self._normalize_text(value)
        return cleaned in self._foreground_window_aliases or self._is_explorer_target(cleaned)

    def _close_foreground_window(self) -> None:
        user32 = ctypes.windll.user32
        foreground_hwnd = user32.GetForegroundWindow()
        if not foreground_hwnd:
            raise PCControlError("Nao encontrei uma janela em foco para fechar.")

        shell_hwnd = user32.GetShellWindow()
        if foreground_hwnd == shell_hwnd:
            raise PCControlError("A area de trabalho esta em foco; nao ha janela segura para fechar.")

        class_name_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(foreground_hwnd, class_name_buffer, len(class_name_buffer))
        class_name = class_name_buffer.value.strip()
        if class_name in {"Progman", "Shell_TrayWnd", "WorkerW"}:
            raise PCControlError("A janela em foco faz parte do shell do Windows e nao sera fechada.")

        WM_SYSCOMMAND = 0x0112
        SC_CLOSE = 0xF060
        if not user32.PostMessageW(foreground_hwnd, WM_SYSCOMMAND, SC_CLOSE, 0):
            raise PCControlError("Nao consegui fechar a janela em foco com seguranca.")

    def _terminate_process(
        self,
        process_name: str,
        *,
        allow_explorer_termination: bool = False,
    ) -> None:
        result = self._run_command(
            ["taskkill", "/IM", process_name, "/F"],
            timeout=10,
            allow_explorer_termination=allow_explorer_termination,
        )
        if result.returncode != 0:
            raise PCControlError(result.stderr.strip() or f"Nao consegui encerrar {process_name}.")

    def _resolve_location(self, location: str | None) -> Path:
        if not location:
            return Path.cwd()

        cleaned = self._normalize_text(location)
        aliases = self._known_locations()
        if cleaned in aliases:
            return aliases[cleaned]

        candidate = Path(location).expanduser()
        if candidate.is_absolute():
            return candidate

        # "documentos/Relatorios" ou "area de trabalho\Projetos": lugar conhecido + subpasta
        head, _, rest = location.replace("\\", "/").partition("/")
        if rest and self._normalize_text(head) in aliases:
            return aliases[self._normalize_text(head)] / rest.strip("/")

        for base in aliases.values():
            named_candidate = base / location
            if named_candidate.exists():
                return named_candidate

        # caminho relativo desconhecido vai para a Área de Trabalho, nunca para dentro da pasta do projeto
        return aliases["desktop"] / location

    def _search_roots(self) -> list[Path]:
        locations = self._known_locations()
        # pastas do usuário primeiro: a pasta da Kira é enorme e gastava todo o tempo de busca
        roots = [
            locations["desktop"],
            locations["downloads"],
            locations["documentos"],
            locations["imagens"],
            Path.cwd(),
            self._user_home(),
        ]
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root).lower()
            if key not in seen and root.exists():
                seen.add(key)
                unique.append(root)
        return unique

    def _matches_name(self, path: Path, name: str) -> bool:
        wanted = self._normalize_text(name)
        filename = self._normalize_text(path.name)
        stem = self._normalize_text(path.stem)
        return filename == wanted or stem == wanted

    def _find_path(self, name: str, expected_type: str | None = None) -> Path:
        cleaned = name.strip().strip("\"'")
        resolution = resolve_operational_memory_target(
            cleaned,
            expected_type=expected_type,
            store=self._local_memory,
        )
        if resolution.status != "skip":
            logger.info("OP_MEMORY_COMMAND_DETECTED input=%r", cleaned)
            logger.info("OP_MEMORY_COMMAND_ACTION expected_type=%s", expected_type or "any")
        if resolution.status == "resolved" and resolution.path:
            path = Path(resolution.path)
            if path.exists():
                logger.info("OP_MEMORY_TARGET_RESOLVED path=%s reason=%s", path, resolution.reason)
                return path
        if resolution.status == "ambiguous":
            logger.info("OP_MEMORY_TARGET_AMBIGUOUS candidates=%s", list(resolution.candidates))
            options = [Path(p).name for p in resolution.candidates[:3] if p]
            if options:
                preview = ", ".join(options)
                raise PCControlError(f"Encontrei mais de um item possivel: {preview}. Me diga qual voce quer.")
            raise PCControlError("Encontrei mais de um item possivel. Me diga qual voce quer.")
        if resolution.status == "deleted":
            logger.info("OP_MEMORY_TARGET_DELETED path=%s", resolution.path)
            deleted_path = resolution.path or "desconhecido"
            raise PCControlError(f"Esse item ja foi apagado. Caminho antigo: {deleted_path}.")
        if resolution.status == "not_found":
            logger.info("OP_MEMORY_TARGET_NOT_FOUND query=%r reason=%s", cleaned, resolution.reason)
            logger.info("OP_MEMORY_COMMAND_FALLBACK query=%r", cleaned)

        direct = Path(cleaned).expanduser()
        if direct.exists():
            if expected_type == "file" and not direct.is_file():
                raise PCControlError(f"{cleaned} existe, mas nao e um arquivo.")
            if expected_type == "folder" and not direct.is_dir():
                raise PCControlError(f"{cleaned} existe, mas nao e uma pasta.")
            return direct

        for root in self._search_roots():
            for child in root.iterdir():
                if self._matches_name(child, cleaned):
                    if expected_type == "file" and not child.is_file():
                        continue
                    if expected_type == "folder" and not child.is_dir():
                        continue
                    return child

        deadline = time.monotonic() + self._search_timeout_seconds
        for root in self._search_roots():
            pattern = cleaned if "." in cleaned else f"{cleaned}*"
            for candidate in self._iter_limited_matches(root, pattern, deadline=deadline):
                if self._matches_name(candidate, cleaned) or cleaned in candidate.name.lower():
                    if expected_type == "file" and not candidate.is_file():
                        continue
                    if expected_type == "folder" and not candidate.is_dir():
                        continue
                    return candidate

        kind = "arquivo" if expected_type == "file" else "pasta" if expected_type == "folder" else "item"
        raise PCControlError(f"Nao encontrei {kind} com o nome '{name}'.")

    def _find_path_in_location(
        self,
        name: str,
        location: str,
        expected_type: str | None = None,
    ) -> Path:
        base = self._resolve_location(location)
        if not base.exists() or not base.is_dir():
            raise PCControlError(f"O local {base} nao existe ou nao e uma pasta.")

        direct = base / name.strip().strip("\"'")
        if direct.exists():
            if expected_type == "file" and not direct.is_file():
                raise PCControlError(f"{direct.name} existe, mas nao e um arquivo.")
            if expected_type == "folder" and not direct.is_dir():
                raise PCControlError(f"{direct.name} existe, mas nao e uma pasta.")
            return direct

        pattern = name.strip().strip("\"'")
        deadline = time.monotonic() + self._search_timeout_seconds
        for candidate in self._iter_limited_matches(base, pattern if "." in pattern else f"{pattern}*", deadline=deadline):
            if not self._matches_name(candidate, pattern) and pattern.lower() not in candidate.name.lower():
                continue
            if expected_type == "file" and not candidate.is_file():
                continue
            if expected_type == "folder" and not candidate.is_dir():
                continue
            return candidate

        kind = "arquivo" if expected_type == "file" else "pasta" if expected_type == "folder" else "item"
        raise PCControlError(f"Nao encontrei {kind} com o nome '{name}' em {base}.")

    def create_folder(self, name: str, location: str | None = None) -> str:
        base = self._resolve_location(location or "desktop")
        path = base / name
        path.mkdir(parents=True, exist_ok=True)
        self._local_memory.record_folder_action(path, action="created", origin="pc_control.create_folder")
        self._local_memory.record_folder_snapshot(path.parent, origin="pc_control.create_folder")
        return f"Pasta criada em {path}."

    def create_file(self, name: str, location: str | None = None, content: str = "") -> str:
        base = self._resolve_location(location or "desktop")
        path = base / (name if Path(name).suffix else f"{name}.txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        if content:
            # nota ditada: nunca sobrescreve, grava e abre no Bloco de Notas
            path = self._dedupe_destination(path)
            path.write_text(content, encoding="utf-8")
            subprocess.Popen(["notepad.exe", str(path)])
        else:
            path.touch(exist_ok=True)
        self._local_memory.record_file_action(path, action="created", origin="pc_control.create_file")
        self._local_memory.record_folder_action(path.parent, action="touched", origin="pc_control.create_file")
        self._local_memory.record_folder_snapshot(path.parent, origin="pc_control.create_file")
        return f"Arquivo criado em {path}."

    def open_item(self, name: str | None = None, item_type: str | None = None, location: str | None = None) -> str:
        if location and name:
            target = self._find_path_in_location(name, location, expected_type=item_type)
        elif location:
            target = self._resolve_location(location)
        elif name:
            target = self._find_path(name, expected_type=item_type)
        else:
            raise PCControlError("Informe o nome do arquivo, pasta ou local para abrir.")

        if not target.exists():
            raise PCControlError(f"O caminho {target} nao existe.")
        target_is_dir = target.is_dir()

        os.startfile(str(target))
        if target.is_file():
            self._local_memory.record_file_action(target, action="opened", origin="pc_control.open_item")
            self._local_memory.record_folder_action(target.parent, action="visited", origin="pc_control.open_item")
            self._local_memory.record_folder_snapshot(target.parent, origin="pc_control.open_item")
        elif target.is_dir():
            self._local_memory.record_folder_action(target, action="opened", origin="pc_control.open_item")
            self._local_memory.record_folder_snapshot(target, origin="pc_control.open_item")
        return f"Abrindo {target}."

    def rename_item(self, current_name: str, new_name: str, item_type: str | None = None) -> str:
        source = self._find_path(current_name, expected_type=item_type)
        if source.is_file() and not Path(new_name).suffix:
            new_name += source.suffix
        destination = source.with_name(new_name)
        if destination.exists():
            raise PCControlError(f"Ja existe um item chamado '{new_name}' na mesma pasta.")
        try:
            source.rename(destination)
        except OSError as exc:
            raise PCControlError(f"Nao consegui renomear '{source.name}': {exc}") from exc
        if destination.is_file():
            self._local_memory.record_delete(source, item_type="file", origin="pc_control.rename_item", action="renamed_old_path")
            self._local_memory.record_file_action(destination, action="renamed", origin="pc_control.rename_item")
        elif destination.is_dir():
            self._local_memory.record_delete(source, item_type="folder", origin="pc_control.rename_item", action="renamed_old_path")
            self._local_memory.record_folder_action(destination, action="renamed", origin="pc_control.rename_item")
        return f"Renomeado para {destination.name}."

    def _run_archiver(self, args: list[str]) -> int:
        try:
            return subprocess.run(
                args, capture_output=True, timeout=240, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ).returncode
        except subprocess.TimeoutExpired:
            return -1

    def compress_item(self, name: str, location: str | None = None, fmt: str = "rar") -> str:
        source = self._find_path_in_location(name, location) if location else self._find_path(name)
        size = source.stat().st_size if source.is_file() else sum(p.stat().st_size for p in source.rglob("*") if p.is_file())
        if size > MAX_ARCHIVE_BYTES:
            raise PCControlError(f"{source.name} passa de 2 GB; compactar por voz travaria o PC. Melhor pelo WinRAR.")
        use_rar = "zip" not in self._normalize_text(fmt)
        if use_rar and not (WINRAR_DIR / "Rar.exe").is_file():
            raise PCControlError("Nao achei o WinRAR instalado; posso compactar em zip.")
        base = source.name if source.is_dir() else source.stem
        target = self._dedupe_destination(source.with_name(f"{base}.{'rar' if use_rar else 'zip'}"))
        if use_rar:
            code = self._run_archiver([str(WINRAR_DIR / "Rar.exe"), "a", "-ep1", "-r", "-idq", "-y", str(target), str(source)])
            if code != 0 or not target.is_file():
                raise PCControlError(f"O WinRAR nao conseguiu compactar {source.name} (codigo {code}).")
        else:
            files = [source] if source.is_file() else sorted(p for p in source.rglob("*") if p.is_file())
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in files:
                    archive.write(path, path.relative_to(source.parent))
        self._local_memory.record_file_action(target, action="created", origin="pc_control.compress_item")
        return f"Compactei {source.name} em {target.name}, na pasta {target.parent}."

    def extract_item(self, name: str, location: str | None = None) -> str:
        source = self._find_path_in_location(name, location, "file") if location else self._find_path(name, "file")
        lower = source.name.lower()
        suffix = ".tgz" if lower.endswith(".tar.gz") else source.suffix.lower()
        if suffix not in ARCHIVE_SUFFIXES:
            raise PCControlError(f"{source.name} nao e arquivo compactado (zip, rar, 7z ou tar).")
        stem = source.name[: -len(".tar.gz")] if lower.endswith(".tar.gz") else source.stem
        target = self._dedupe_destination(source.with_name(stem))
        target.mkdir()
        try:
            if suffix == ".zip":
                root = target.resolve()
                with zipfile.ZipFile(source) as archive:
                    if any(root not in (root / member).resolve().parents for member in archive.namelist()):
                        raise PCControlError(f"{source.name} tenta gravar fora da pasta; nao vou abrir esse.")
                    archive.extractall(target)
            elif suffix == ".rar":
                if not (WINRAR_DIR / "UnRAR.exe").is_file():
                    raise PCControlError("Nao achei o WinRAR instalado para abrir .rar.")
                if self._run_archiver([str(WINRAR_DIR / "UnRAR.exe"), "x", "-o-", "-p-", "-idq", "-y", str(source), f"{target}{os.sep}"]) != 0:
                    raise PCControlError(f"Nao consegui descompactar {source.name}; pode ter senha ou estar corrompido.")
            # tar do Windows (libarchive) recusa caminhos absolutos e '..' por padrao
            elif self._run_archiver(["tar", "-xf", str(source), "-C", str(target)]) != 0:
                raise PCControlError(f"Nao consegui descompactar {source.name}.")
        except BaseException:
            if not any(target.iterdir()):
                target.rmdir()
            raise
        self._local_memory.record_folder_action(target, action="created", origin="pc_control.extract_item")
        return f"Descompactei {source.name} na pasta {target.name}, em {target.parent}."

    def delete_item(
        self,
        name: str | None = None,
        item_type: str | None = None,
        location: str | None = None,
        recycle: bool = True,
    ) -> str:
        if location and not name:
            target = self._resolve_location(location)
        elif name and location:
            target = self._find_path_in_location(name, location, expected_type=item_type)
        elif name:
            target = self._find_path(name, expected_type=item_type)
        else:
            raise PCControlError("Informe o item que deve ser excluido.")

        if not target.exists():
            raise PCControlError(f"O caminho {target} nao existe (nada foi apagado).")
        # Tipo resolvido ANTES do efeito: depois de apagar nao da mais para perguntar.
        target_is_dir = target.is_dir()

        def _audit(action: str) -> None:
            # Falha de registro nunca pode virar "erro" de uma exclusao que ja aconteceu.
            try:
                self._local_memory.record_delete(target, item_type="folder" if target_is_dir else "file", origin="pc_control.delete_item", action=action)
            except Exception:
                logger.exception("PC_DELETE_AUDIT_FAILED path=%s action=%s (operacao concluida)", target, action)

        if recycle:
            escaped = self._escape_ps(str(target))
            if target_is_dir:
                script = (
                    "Add-Type -AssemblyName Microsoft.VisualBasic; "
                    "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
                    f"'{escaped}','OnlyErrorDialogs','SendToRecycleBin')"
                )
            else:
                script = (
                    "Add-Type -AssemblyName Microsoft.VisualBasic; "
                    "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
                    f"'{escaped}','OnlyErrorDialogs','SendToRecycleBin')"
                )
            result = self._run_powershell(script, timeout=15)
            if result.returncode != 0:
                if not target.exists():
                    _audit("deleted_to_recycle_bin")
                    return f"Item movido para a lixeira: {target.name} (o Windows reportou aviso, mas o item saiu de {target.parent})."
                raise PCControlError((result.stderr.strip() or "Falha ao mover para a lixeira.") + " O item continua no lugar.")
            _audit("deleted_to_recycle_bin")
            return f"Item movido para a lixeira: {target.name} (estava em {target.parent})."

        if target_is_dir:
            shutil.rmtree(target)
        else:
            target.unlink()
        _audit("deleted")
        return f"Item removido: {target.name}."

    def move_or_copy_item(
        self,
        source_name: str,
        destination: str,
        operation: str,
        item_type: str | None = None,
    ) -> str:
        source = self._find_path(source_name, expected_type=item_type)
        destination_dir = self._resolve_location(destination)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination_path = destination_dir / source.name

        if operation == "mover":
            if destination_path.exists():
                raise PCControlError(f"Ja existe {destination_path}; nada foi movido.")
            shutil.move(str(source), str(destination_path))
            try:
                if destination_path.exists():
                    if destination_path.is_file():
                        self._local_memory.record_delete(source, item_type="file", origin="pc_control.move_or_copy_item", action="moved_from_old_path")
                        self._local_memory.record_file_action(destination_path, action="moved", origin="pc_control.move_or_copy_item")
                    else:
                        self._local_memory.record_delete(source, item_type="folder", origin="pc_control.move_or_copy_item", action="moved_from_old_path")
                        self._local_memory.record_folder_action(destination_path, action="moved", origin="pc_control.move_or_copy_item")
            except Exception:
                logger.exception("PC_MOVE_AUDIT_FAILED src=%s dst=%s (operacao concluida)", source, destination_path)
            return f"Item movido de {source.parent} para {destination_path}."

        if source.is_dir():
            if destination_path.exists():
                raise PCControlError(f"A pasta de destino {destination_path} ja existe.")
            shutil.copytree(source, destination_path)
        else:
            shutil.copy2(source, destination_path)
        if destination_path.exists():
            if destination_path.is_file():
                self._local_memory.record_file_action(destination_path, action="copied", origin="pc_control.move_or_copy_item")
            else:
                self._local_memory.record_folder_action(destination_path, action="copied", origin="pc_control.move_or_copy_item")
        return f"Item copiado para {destination_path}."

    def organize_folder(
        self,
        location: str,
        mode: str = "tipo",
    ) -> str:
        target_dir = self._resolve_location(location)
        if not target_dir.exists() or not target_dir.is_dir():
            raise PCControlError(f"Pasta nao encontrada: {target_dir}")

        if self._normalize_text(mode) != "tipo":
            raise PCControlError("Modo de organizacao invalido. Use tipo.")

        categories = {
            "Imagens": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg"},
            "Videos": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm"},
            "Documentos": {".pdf", ".docx", ".doc", ".txt", ".xlsx", ".xls", ".pptx", ".csv"},
            "Compactados": {".zip", ".rar", ".7z", ".tar", ".gz"},
            "Musicas": {".mp3", ".wav", ".flac", ".aac", ".ogg"},
        }

        moved_count = 0
        deadline = time.monotonic() + self._organize_timeout_seconds
        for child in target_dir.iterdir():
            if time.monotonic() > deadline:
                return (
                    f"Organizacao parcial concluida: {moved_count} arquivo(s) movido(s) em {target_dir}. "
                    "A operacao atingiu o limite de tempo."
                )
            if not child.is_file():
                continue

            extension = child.suffix.lower()
            destination_folder = None
            for folder_name, extensions in categories.items():
                if extension in extensions:
                    destination_folder = target_dir / folder_name
                    break

            if destination_folder is None:
                continue

            destination_folder.mkdir(parents=True, exist_ok=True)
            destination_path = self._dedupe_destination(destination_folder / child.name)
            shutil.move(str(child), str(destination_path))
            moved_count += 1

        return f"Pasta organizada: {moved_count} arquivo(s) movido(s) em {target_dir}."

    def search_files_by_name(
        self,
        search_name: str,
        location: str | None = None,
        item_type: str = "arquivo",
        limit: int = 10,
    ) -> str:
        if limit <= 0:
            raise PCControlError("O limite de busca precisa ser maior que zero.")

        base_dir = self._resolve_location(location) if location else None
        roots = [base_dir] if base_dir else self._search_roots()
        needle = self._normalize_text(search_name)
        if not needle:
            raise PCControlError("Informe o nome que deve ser buscado.")

        results: list[Path] = []
        deadline = time.monotonic() + self._search_timeout_seconds
        for root in roots:
            if root is None or not root.exists():
                continue
            pattern = f"*{search_name.strip()}*" if search_name.strip() else "*"
            for candidate in self._iter_limited_matches(root, pattern, deadline=deadline):
                if item_type == "arquivo" and not candidate.is_file():
                    continue
                if item_type in {"pasta", "folder"} and not candidate.is_dir():
                    continue
                if needle in candidate.name.lower():
                    results.append(candidate)
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break

        if not results:
            kind = "arquivo" if item_type == "arquivo" else "pasta"
            raise PCControlError(f"Nenhum {kind} encontrado com '{search_name}'.")

        lines = [f"Encontrei {len(results)} resultado(s):"]
        for path in results:
            lines.append(str(path))
        return "\n".join(lines)

    def _dedupe_destination(self, path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        counter = 1
        while True:
            candidate = path.with_name(f"{stem} ({counter}){suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    def _iter_limited_matches(
        self,
        root: Path,
        pattern: str,
        *,
        deadline: float,
    ):
        root_depth = len(root.parts)
        for current_root, dirs, files in os.walk(root):
            if time.monotonic() > deadline:
                break
            current_path = Path(current_root)
            current_depth = len(current_path.parts) - root_depth
            if current_depth >= self._search_max_depth:
                dirs[:] = []
            names = list(dirs) + list(files)
            for name in names:
                if time.monotonic() > deadline:
                    return
                if not Path(name).match(pattern):
                    continue
                yield current_path / name

    def set_volume_percent(self, percent: int) -> str:
        return self.set_volume_absolute(percent)

    def _set_volume_os(self, value: int) -> None:
        """Set the active Windows playback endpoint volume."""
        logger.info("VOLUME_BACKEND_SET_BEGIN value=%s", value)
        try:
            with _volume_com_scope():
                endpoint = self._get_default_audio_endpoint()
                endpoint.SetMasterVolumeLevelScalar(max(0.0, min(1.0, float(value) / 100.0)), None)
                if value > 0:
                    endpoint.SetMute(0, None)
            logger.info("VOLUME_BACKEND_SET_OK value=%s", value)
        except Exception as exc:
            logger.warning("VOLUME_BACKEND_SET_FAILED value=%s error=%s", value, exc)
            raise

    @staticmethod
    def _get_default_audio_endpoint():
        try:
            from ctypes import POINTER, cast
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        except ImportError as exc:
            raise PCControlError("Backend de volume do endpoint ativo indisponivel.") from exc
        device = AudioUtilities.GetSpeakers()
        endpoint = getattr(device, "EndpointVolume", None)
        if endpoint is not None:
            return endpoint
        interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(interface, POINTER(IAudioEndpointVolume))

    def _read_volume_os(self) -> tuple[int, bool]:
        with _volume_com_scope():
            endpoint = self._get_default_audio_endpoint()
            percent = int(round(float(endpoint.GetMasterVolumeLevelScalar()) * 100))
            muted = bool(endpoint.GetMute())
        return max(0, min(100, percent)), muted

    def _set_mute_os(self, muted: bool) -> None:
        with _volume_com_scope():
            self._get_default_audio_endpoint().SetMute(1 if muted else 0, None)

    @staticmethod
    def _press_media_key(key: str, presses: int = 1) -> bool:
        try:
            import pyautogui

            for _ in range(max(1, int(presses))):
                pyautogui.press(key)
            return True
        except Exception as exc:
            logger.warning("volume_media_key_fallback_failed key=%s error=%s", key, exc)
            return False

    def get_volume_state(self) -> dict[str, int | bool]:
        with self._volume_state_lock:
            return dict(self._volume_state)

    def set_volume_absolute(self, percent: int) -> str:
        requested = int(percent)
        clamped = max(0, min(100, requested))
        logger.info('VOLUME_COMMAND_MATCHED text="" action=set value=%s', clamped)
        logger.info("volume_command_received action=set_volume_absolute requested_value=%s", requested)
        logger.info("volume_absolute_requested requested_value=%s clamped_value=%s", requested, clamped)
        try:
            before, _before_muted = self._read_volume_os()
            logger.info("VOLUME_REAL_BEFORE percent=%s", before)
            logger.info("VOLUME_REAL_APPLY method=pycaw")
            self._set_volume_os(clamped)
            actual, muted = self._read_volume_os()
        except Exception as exc:
            logger.info("VOLUME_APPLY_FAILED expected=%s actual=unknown reason=%s", clamped, exc)
            logger.exception("volume_command_failed action=set_volume_absolute requested_value=%s clamped_value=%s", requested, clamped)
            raise PCControlError("Nao consegui alterar o volume pelo backend atual.") from exc
        with self._volume_state_lock:
            self._volume_state["current_volume_percent"] = actual
            self._volume_state["is_muted"] = muted
            if actual > 0:
                self._volume_state["last_nonzero_volume_percent"] = actual
            state = dict(self._volume_state)
        logger.info("volume_applied volume=%s muted=%s", state["current_volume_percent"], state["is_muted"])
        logger.info("volume_ui_synced volume=%s muted=%s", state["current_volume_percent"], state["is_muted"])
        logger.info("VOLUME_REAL_AFTER percent=%s", actual)
        if abs(int(actual) - int(clamped)) <= 1:
            logger.info("VOLUME_APPLY_OK expected=%s actual=%s", clamped, actual)
        else:
            logger.warning("VOLUME_APPLY_FAILED expected=%s actual=%s reason=confirmed_value_mismatch", clamped, actual)
            raise PCControlError(f"Volume real ficou em {actual}%, esperado {clamped}%.")
        return f"Interface: volume em {actual}%."

    def adjust_volume_relative(self, direction: str, step: str = "default") -> str:
        step_map = {"small": 5, "default": 10, "large": 20}
        delta = step_map.get(str(step or "default").strip().lower(), 10)
        direction_norm = str(direction or "").strip().lower()
        if direction_norm not in {"up", "down"}:
            raise PCControlError("Direcao de volume invalida. Use up ou down.")
        try:
            current, muted = self._read_volume_os()
            logger.info("VOLUME_COMMAND_MATCHED text=\"\" action=%s value=%s", "increase" if direction_norm == "up" else "decrease", delta)
            logger.info("VOLUME_REAL_BEFORE percent=%s", current)
        except Exception as exc:
            key = "volumeup" if direction_norm == "up" else "volumedown"
            if self._press_media_key(key, max(1, delta // 2)):
                logger.warning("volume_confirmation_unavailable fallback=media_key direction=%s error=%s", direction_norm, exc)
                return "Interface: enviei o ajuste de volume, mas nao consegui confirmar o nivel."
            raise PCControlError("Nao consegui ler o volume pelo backend atual.") from exc
        with self._volume_state_lock:
            self._volume_state["current_volume_percent"] = current
            self._volume_state["is_muted"] = muted
        target = current + delta if direction_norm == "up" else current - delta
        logger.info("volume_relative_requested direction=%s step=%s current=%s target=%s", direction_norm, step, current, target)
        result = self.set_volume_absolute(target)
        return result

    def mute_volume(self) -> str:
        logger.info("volume_command_received action=mute_volume")
        try:
            current, _ = self._read_volume_os()
            logger.info('VOLUME_COMMAND_MATCHED text="" action=mute value=')
            logger.info("VOLUME_REAL_BEFORE percent=%s", current)
            logger.info("VOLUME_REAL_APPLY method=pycaw")
            self._set_mute_os(True)
            actual, muted = self._read_volume_os()
        except Exception as exc:
            logger.info("VOLUME_APPLY_FAILED expected=mute actual=unknown reason=%s", exc)
            if self._press_media_key("volumemute"):
                logger.warning("volume_confirmation_unavailable fallback=media_key action=mute error=%s", exc)
                return "Interface: enviei o comando de mute, mas nao consegui confirmar o estado."
            raise PCControlError("Nao consegui alterar o volume pelo backend atual.") from exc
        with self._volume_state_lock:
            if current > 0:
                self._volume_state["last_nonzero_volume_percent"] = current
            self._volume_state["current_volume_percent"] = actual
            self._volume_state["is_muted"] = muted
        if not muted:
            raise PCControlError("Nao consegui confirmar o mute no endpoint ativo.")
        logger.info("VOLUME_REAL_AFTER percent=%s", actual)
        logger.info("VOLUME_APPLY_OK expected=mute actual=%s", str(muted).lower())
        logger.info("volume_muted")
        return "Interface: som mutado."

    def unmute_volume(self) -> str:
        with self._volume_state_lock:
            restore = int(self._volume_state.get("last_nonzero_volume_percent", 50) or 50)
            if restore <= 0:
                restore = 50
        logger.info("volume_command_received action=unmute_volume restore=%s", restore)
        try:
            current, _ = self._read_volume_os()
            logger.info('VOLUME_COMMAND_MATCHED text="" action=unmute value=')
            logger.info("VOLUME_REAL_BEFORE percent=%s", current)
            logger.info("VOLUME_REAL_APPLY method=pycaw")
            if current <= 0:
                self._set_volume_os(restore)
            self._set_mute_os(False)
            actual, muted = self._read_volume_os()
        except Exception as exc:
            logger.info("VOLUME_APPLY_FAILED expected=unmute actual=unknown reason=%s", exc)
            if self._press_media_key("volumemute"):
                logger.warning("volume_confirmation_unavailable fallback=media_key action=unmute error=%s", exc)
                return "Interface: enviei o comando para ativar o som, mas nao consegui confirmar o estado."
            raise PCControlError("Nao consegui alterar o volume pelo backend atual.") from exc
        if muted:
            raise PCControlError("Nao consegui confirmar a ativacao do som no endpoint ativo.")
        with self._volume_state_lock:
            self._volume_state["current_volume_percent"] = actual
            self._volume_state["is_muted"] = False
            if actual > 0:
                self._volume_state["last_nonzero_volume_percent"] = actual
        logger.info("volume_unmuted restored=%s", actual)
        logger.info("VOLUME_REAL_AFTER percent=%s", actual)
        logger.info("VOLUME_APPLY_OK expected=unmute actual=%s", str(muted).lower())
        return f"Interface: som ativo em {actual}%."

    def control_volume(self, action: str, percent: int | None = None) -> str:
        """Controla volume: ações relativas ou percentual absoluto."""
        # Modo absoluto: "definir 50" ou via percent
        if percent is not None:
            return self.set_volume_absolute(percent)
        try:
            pct = int(action)
            return self.set_volume_absolute(pct)
        except (ValueError, TypeError):
            pass

        action_map = {
            "aumentar": ("up", "default"),
            "diminuir": ("down", "default"),
            "aumentar_pouco": ("up", "small"),
            "diminuir_pouco": ("down", "small"),
            "maximo": ("set", "100"),
            "silenciar": ("mute", ""),
            "dessilenciar": ("unmute", ""),
            "desmutar": ("unmute", ""),
        }
        normalized = str(action or "").strip().lower()
        if normalized not in action_map:
            raise PCControlError(f"Acao de volume invalida: '{action}'. Use aumentar, diminuir, maximo, silenciar ou um numero de 0 a 100.")
        kind, data = action_map[normalized]
        if kind == "set":
            return self.set_volume_absolute(int(data))
        if kind == "mute":
            return self.mute_volume()
        if kind == "unmute":
            return self.unmute_volume()
        return self.adjust_volume_relative(kind, data)

    def control_power(self, action: str) -> str:
        if action == "bloquear":
            ctypes.windll.user32.LockWorkStation()
            return "PC bloqueado."

        if action == "desligar":
            subprocess.Popen(["shutdown", "/s", "/t", "0"])
            return "Desligando o PC."

        if action == "reiniciar":
            subprocess.Popen(["shutdown", "/r", "/t", "0"])
            return "Reiniciando o PC."

        if action == "suspender":
            result = self._run_powershell(
                "Add-Type -AssemblyName System.Windows.Forms; "
                "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)",
                timeout=10,
            )
            if result.returncode != 0:
                raise PCControlError(result.stderr.strip() or "Falha ao suspender o PC.")
            return "Colocando o PC em suspensao."

        raise PCControlError("Acao de energia invalida.")

    def open_web(self, target: str, query: str | None = None) -> str:
        # FASE 0 — caminho LOCAL apenas (sem roteamento interno legado). O navegador
        # embutido (aba Internet) e tratado pelo fluxo novo via Browser Agent externo.
        cleaned = self._normalize_text(target)
        command_text = f"{target} {query or ''}".strip()

        # Pesquisa explícita
        if cleaned == "pesquisar":
            if not query:
                raise PCControlError("Informe o texto da pesquisa.")
            url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
            self.open_in_local_browser(url, command_text)
            return f"Pesquisando por '{query}'."

        # Alias exato registrado
        if cleaned in self._web_aliases:
            url = self._web_aliases[cleaned]
            self.open_in_local_browser(url, command_text)
            return f"Abrindo {cleaned}."

        self._require_valid_web_target(target)
        # Resolve via camada inteligente de navegação
        nav = self._resolve_navigation(target)
        url = str(nav.get("resolved_url") or self._resolve_web_target(cleaned))
        return self.open_in_local_browser(url, command_text)

    def open_program_with_target(self, program_name: str, destination: str) -> str:
        cleaned_program = self._normalize_text(program_name)
        nav = self._resolve_navigation(destination)
        url = str(nav.get("resolved_url") or self._resolve_web_target(destination))

        if cleaned_program in {"chrome", "google chrome"}:
            chrome_paths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]
            for path in chrome_paths:
                if os.path.exists(path):
                    logger.info('browser_route_opening_external_browser command="%s" url="%s"', f"{program_name} {destination}".strip(), url)
                    logger.info('browser_route_external_nav_requested url="%s"', url)
                    subprocess.Popen([path, url])
                    return f"Abrindo Chrome em {url}."

        if cleaned_program in {"edge", "microsoft edge"}:
            logger.info('browser_route_opening_external_browser command="%s" url="%s"', f"{program_name} {destination}".strip(), url)
            logger.info('browser_route_external_nav_requested url="%s"', url)
            subprocess.Popen(["cmd", "/c", "start", "", "msedge", url])
            return f"Abrindo Edge em {url}."

        self.control_program("abrir", program_name)
        self._open_external_browser(url, f"{program_name} {destination}".strip())
        return f"Abrindo {program_name} e carregando {url} no navegador padrao."

    def open_direct_link(self, site: str) -> str:
        self._require_valid_web_target(site)
        nav = self._resolve_navigation(site)
        url = str(nav.get("resolved_url") or self._resolve_web_target(site))
        return self.open_in_local_browser(url, site)

    def open_app_or_link(
        self,
        program: str | None = None,
        destination: str | None = None,
        site: str | None = None,
    ) -> str:
        if program and destination:
            return self.open_program_with_target(program, destination)
        if site:
            return self.open_direct_link(site)
        if destination:
            return self.open_direct_link(destination)
        if program:
            return self.control_program("abrir", program)
        raise PCControlError("Nada para abrir.")

    def open_app_or_link_from_text(self, command_text: str) -> str:
        original = command_text.strip()
        cleaned = self._normalize_text(original)

        if not cleaned:
            raise PCControlError("Informe o que deve ser aberto.")

        if any(token in cleaned for token in ("pausa a musica", "pausar musica", "continua a musica", "retoma a musica", "play musica", "pause musica")):
            return self._send_music_command("play_pause")
        if any(token in cleaned for token in ("proxima musica", "próxima música", "musica seguinte", "avanca musica", "avançar música")):
            return self._send_music_command("next")
        if any(token in cleaned for token in ("musica anterior", "música anterior", "volta musica", "voltar música")):
            return self._send_music_command("previous")
        volume_up_match = re.search(r"(aumenta|aumentar)\s+o?\s*volume", cleaned)
        volume_down_match = re.search(r"(abaixa|diminu(i|ir))\s+o?\s*volume", cleaned)
        if volume_up_match:
            return self._send_music_command("set_volume", 85)
        if volume_down_match:
            return self._send_music_command("set_volume", 35)

        if self._looks_like_music_request(original):
            query = self._extract_music_query(original)
            # Música deve priorizar player interno da Kira.
            return self._open_internal_music(query, original)

        nav = self._resolve_navigation(original)
        resolved_url = str(nav.get("resolved_url") or "")
        if resolved_url and str(nav.get("platform")) in {
            "youtube",
            "instagram",
            "google",
            "gmail",
            "spotify",
            "whatsapp_web",
            "generic",
        }:
            return self.open_in_local_browser(resolved_url, original)

        site_match = re.search(r"\bsite d[ao]\s+(.+)$", cleaned)
        if site_match:
            return self.open_direct_link(site_match.group(1).strip())

        app_with_target_match = re.search(
            r"\babre(?:\s+o|\s+a)?\s+(.+?)\s+no\s+(.+)$",
            cleaned,
        )
        if app_with_target_match:
            program = app_with_target_match.group(1).strip()
            destination = app_with_target_match.group(2).strip()
            return self.open_program_with_target(program, destination)

        open_match = re.search(r"\babre(?:\s+o|\s+a)?\s+(.+)$", cleaned)
        if open_match:
            target = open_match.group(1).strip()
            if target in self._app_aliases:
                return self.control_program("abrir", target)
            return self.open_direct_link(target)

        raise PCControlError("Nao consegui entender o que abrir.")

    def _resolve_web_target(self, target: str) -> str:
        cleaned = self._normalize_text(target)
        cleaned = re.sub(r"^site d[ao]\s+", "", cleaned).strip()

        if cleaned in self._web_aliases:
            return self._web_aliases[cleaned]
        if cleaned.startswith("http://") or cleaned.startswith("https://"):
            return cleaned
        if "." in cleaned:
            return f"https://{cleaned}" if not cleaned.startswith("www.") else f"https://{cleaned}"
        raise PCControlError("Interface: nao identifiquei um site valido para abrir.")

    def _require_valid_web_target(self, target: str) -> None:
        raw_target = str(target or "").strip()
        cleaned = self._normalize_text(raw_target)
        file_terms = (
            "paste", "pasta", "arquivo", "pdf", "de breve", "pra mim", "para mim",
            "onde ta", "onde esta", "documentos", "downloads", "area de trabalho", "desktop",
        )
        if any(term in cleaned for term in file_terms):
            logger.info("OPEN_WEB_TARGET_REJECTED reason=local_file_phrase target=%r", cleaned)
            raise PCControlError("Interface: nao identifiquei um site valido para abrir.")
        if cleaned in self._web_aliases:
            return
        if re.match(r"^https?://[^\s]+\.[^\s]+$", raw_target, flags=re.IGNORECASE):
            return
        if re.match(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:[/#:?].*)?$", raw_target, flags=re.IGNORECASE):
            return
        if " " in cleaned:
            logger.info("OPEN_WEB_TARGET_REJECTED reason=ambiguous_natural_phrase target=%r", cleaned)
        else:
            logger.info("OPEN_WEB_TARGET_REJECTED reason=no_domain_or_known_alias target=%r", cleaned)
        raise PCControlError("Interface: nao identifiquei um site valido para abrir.")

    def _build_youtube_channel_url(self, channel_name: str) -> str:
        """Tenta montar URL direta do canal; se não possível, usa busca."""
        cleaned = channel_name.strip()
        # Se já parece um @handle, vai direto
        if cleaned.startswith("@"):
            return f"https://www.youtube.com/{cleaned}"
        # Remove espaços e caracteres especiais para tentar @handle
        handle_candidate = re.sub(r"[^a-zA-Z0-9_.-]", "", cleaned.replace(" ", ""))
        if handle_candidate and len(handle_candidate) >= 2:
            # Abre busca de canal que normalmente mostra o canal como 1º resultado
            query = urllib.parse.quote_plus(cleaned)
            return f"https://www.youtube.com/results?search_query={query}&sp=EgIQAg%3D%3D"
        query = urllib.parse.quote_plus(f"canal {cleaned}")
        return f"https://www.youtube.com/results?search_query={query}"

    def _build_youtube_channel_search_url(self, channel_name: str) -> str:
        return self._build_youtube_channel_url(channel_name)

    def control_program(self, action: str, program_name: str) -> str:
        cleaned = self._normalize_text(program_name)
        shortcut_target = self._find_program_shortcut(program_name)
        target = str(shortcut_target) if shortcut_target else self._app_aliases.get(cleaned, program_name)
        normalized_action = self._normalize_text(action)

        if normalized_action == "abrir":
            try:
                os.startfile(target)
            except OSError:
                try:
                    subprocess.Popen([target])
                except FileNotFoundError as exc:
                    shortcuts_path = str(self._shortcuts_dir)
                    raise PCControlError(
                        f"Nao consegui abrir '{program_name}'. Coloque um atalho dele em {shortcuts_path}."
                    ) from exc
            if shortcut_target:
                return f"Abrindo {program_name} pelo atalho em {self._shortcuts_dir}."
            return f"Abrindo {program_name}."

        if normalized_action == "fechar":
            if self._should_close_foreground_window(cleaned):
                self._close_foreground_window()
                if cleaned in {"pasta", "pasta atual", "pasta em foco", "essa pasta"}:
                    return "Fechando a pasta em foco."
                return "Fechando a janela em foco."

            # "Fechar" fecha a janela (como o X); encerrar o processo é só com "encerra/mata".
            try:
                from runtime.desktop_windows import window_action
            except ImportError:  # pragma: no cover
                from agent.runtime.desktop_windows import window_action
            return window_action("fechar", program_name)

        if normalized_action in {"encerrar", "matar"}:
            process_name = self._resolve_process_name(program_name)
            self._terminate_process(
                process_name,
                allow_explorer_termination=self._is_explorer_target(process_name),
            )
            return f"Encerrando {program_name}."

        if normalized_action == "reiniciar":
            process_name = self._resolve_process_name(program_name)
            if not self._is_explorer_target(process_name):
                raise PCControlError("Reiniciar programa por esta ferramenta so e suportado para o Explorer.")
            self._terminate_process(process_name, allow_explorer_termination=True)
            subprocess.Popen(["explorer.exe"])
            return "Reiniciando o Explorer."

        raise PCControlError("Acao de programa invalida.")

    def current_time(self) -> str:
        return now_brasilia().strftime("Agora sao %H:%M.")

    def start_stopwatch(self) -> str:
        self._stopwatch_started_at = time.monotonic()
        return "Cronometro iniciado."

    def stopwatch_status(self) -> str:
        if self._stopwatch_started_at is None:
            raise PCControlError("Nenhum cronometro esta em andamento.")
        elapsed = int(time.monotonic() - self._stopwatch_started_at)
        return f"Cronometro rodando ha {elapsed} segundos."

    def start_timer(self, seconds: int) -> str:
        if seconds <= 0:
            raise PCControlError("O temporizador precisa ser maior que zero.")

        def timer_done() -> None:
            try:
                for _ in range(3):
                    ctypes.windll.user32.MessageBeep(0xFFFFFFFF)
                    time.sleep(0.2)
            except Exception:
                pass

        threading.Timer(seconds, timer_done).start()
        return f"Temporizador iniciado por {seconds} segundos."

    def _kira_screenshots_dir(self) -> Path:
        home = self._user_home()
        kira_dir = home / "Documents" / "Kira" / "Screenshots"
        kira_dir.mkdir(parents=True, exist_ok=True)
        return kira_dir

    def _desktop_dir(self) -> Path:
        user_profile = Path(os.environ.get("USERPROFILE", str(self._user_home())))
        candidates = [user_profile / "Desktop", self._user_home() / "Desktop", self._user_home() / "Área de Trabalho"]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    def _downloads_dir(self) -> Path:
        return self._user_home() / "Downloads"

    def _documents_dir(self) -> Path:
        return self._user_home() / "Documents"

    def _sanitize_screenshot_filename(self, value: str | None) -> str:
        raw = (value or "").strip()
        if not raw:
            raw = f"screenshot_{now_brasilia().strftime('%Y%m%d_%H%M%S')}"
        normalized = "".join(ch for ch in unicodedata.normalize("NFD", raw) if unicodedata.category(ch) != "Mn")
        normalized = re.sub(r'[\\/:*?"<>|]+', "_", normalized)
        normalized = re.sub(r"\s+", "_", normalized).strip("._ ")
        normalized = re.sub(r"_+", "_", normalized)
        if not normalized:
            normalized = f"screenshot_{uuid.uuid4().hex[:8]}"
        if "." not in normalized:
            normalized = f"{normalized}.png"
        else:
            base, ext = os.path.splitext(normalized)
            if not ext:
                normalized = f"{base}.png"
            elif ext.lower() != ".png":
                normalized = f"{base}.png"
        return normalized

    def _parse_screenshot_request(self, save_path: str | None) -> tuple[str, str, bool]:
        if not save_path:
            return "", "", False

        raw = str(save_path).strip().strip("\"'")
        normalized = self._normalize_text(raw)

        if re.search(r"[\\/]", raw) or re.match(r"^[a-zA-Z]:\\", raw) or raw.startswith("~"):
            path_like = Path(raw).expanduser()
            if path_like.suffix:
                return path_like.name, str(path_like.parent), True
            return "", str(path_like), True

        dir_aliases = {
            "desktop": str(self._desktop_dir()),
            "area de trabalho": str(self._desktop_dir()),
            "downloads": str(self._downloads_dir()),
            "documentos": str(self._documents_dir()),
            "documents": str(self._documents_dir()),
        }
        for alias, target_dir in dir_aliases.items():
            if alias in normalized:
                name_match = re.search(r"(?:nome|como)\s+(.+)$", normalized)
                requested_name = name_match.group(1).strip() if name_match else ""
                return requested_name, target_dir, False

        if normalized.startswith("nome ") or normalized.startswith("como "):
            requested_name = re.sub(r"^(nome|como)\s+", "", normalized).strip()
            return requested_name, "", False
        if normalized.startswith("salva como ") or normalized.startswith("salvar como "):
            requested_name = re.sub(r"^(salva|salvar)\s+como\s+", "", normalized).strip()
            return requested_name, "", False

        if raw.lower().endswith(".png"):
            return raw, "", False

        return raw, "", False

    def _resolve_screenshot_path(self, requested_name: str, requested_dir: str, from_explicit_path: bool) -> Path:
        logger.info(
            "screenshot_path_resolved requested_name=%s requested_dir=%s from_explicit_path=%s",
            requested_name,
            requested_dir,
            from_explicit_path,
        )

        default_dir = self._kira_screenshots_dir()
        if requested_dir:
            destination = Path(requested_dir).expanduser()
            if not destination.is_absolute():
                destination = (Path.cwd() / destination).resolve()
        else:
            destination = default_dir

        try:
            destination.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.exception("screenshot_path_invalid requested_dir=%s error_message=%s", requested_dir, exc)
            raise PCControlError(f"Diretorio de destino invalido: {requested_dir}") from exc

        safe_name = self._sanitize_screenshot_filename(requested_name or "")
        target = destination / safe_name

        if target.exists():
            stamp = now_brasilia().strftime("%Y%m%d_%H%M%S")
            target = destination / f"{target.stem}_{stamp}.png"
        return target

    def _capture_screenshot(self) -> Image.Image:
        logger.info("screenshot_capture_started")
        try:
            image = ImageGrab.grab(all_screens=True)
            if not isinstance(image, Image.Image):
                raise RuntimeError("capture_returned_non_image")
            width, height = image.size
            if width <= 0 or height <= 0:
                raise RuntimeError("capture_invalid_dimensions")
            logger.info("screenshot_capture_success image_width=%s image_height=%s", width, height)
            return image
        except Exception as exc:
            logger.exception("screenshot_capture_failed error_message=%s", exc)
            raise PCControlError("Falha ao capturar a tela.") from exc

    def _save_screenshot(self, image: Image.Image, output_path: Path) -> dict[str, int]:
        logger.info("screenshot_save_started resolved_path=%s", output_path)
        try:
            image.save(output_path, format="PNG")
        except Exception as exc:
            logger.exception("screenshot_save_failed resolved_path=%s error_message=%s", output_path, exc)
            raise PCControlError(f"Nao consegui salvar o print em {output_path}.") from exc

        try:
            if not output_path.exists():
                raise RuntimeError("file_not_created")
            file_size = int(output_path.stat().st_size)
            if file_size <= 0:
                raise RuntimeError("file_empty")
            with Image.open(output_path) as check_img:
                check_img.verify()
            with Image.open(output_path) as check_img2:
                width, height = check_img2.size
            if width <= 0 or height <= 0:
                raise RuntimeError("invalid_dimensions_after_save")
            logger.info(
                "screenshot_save_success resolved_path=%s file_size=%s image_width=%s image_height=%s",
                output_path,
                file_size,
                width,
                height,
            )
            return {"file_size": file_size, "image_width": int(width), "image_height": int(height)}
        except Exception as exc:
            logger.exception("screenshot_save_validation_failed resolved_path=%s error_message=%s", output_path, exc)
            try:
                if output_path.exists():
                    output_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise PCControlError(f"Arquivo de print invalido/corrompido: {output_path}") from exc

    def _copy_screenshot_to_clipboard(self, image: Image.Image) -> None:
        logger.info("screenshot_clipboard_started")
        tmp_path = Path(tempfile.gettempdir()) / f"kira_clipboard_{uuid.uuid4().hex}.png"
        try:
            image.save(tmp_path, format="PNG")
            escaped = self._escape_ps(str(tmp_path))
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                f"$img = [System.Drawing.Image]::FromFile('{escaped}'); "
                "[System.Windows.Forms.Clipboard]::SetImage($img); "
                "$img.Dispose();"
            )
            result = self._run_powershell(script, timeout=10)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "clipboard_set_failed")
            logger.info("screenshot_clipboard_success")
        except Exception as exc:
            logger.exception("screenshot_clipboard_failed error_message=%s", exc)
            raise PCControlError("Nao consegui copiar o print para o clipboard.") from exc
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _execute_screenshot(self, save_path: str | None, copy_to_clipboard: bool) -> ScreenshotExecutionResult:
        requested_name, requested_dir, from_explicit_path = self._parse_screenshot_request(save_path)
        result = ScreenshotExecutionResult(
            ok=False,
            captured=False,
            saved=False,
            clipboard=False,
            requested_name=requested_name,
            requested_dir=requested_dir,
        )
        try:
            image = self._capture_screenshot()
            result.captured = True
            target = self._resolve_screenshot_path(requested_name, requested_dir, from_explicit_path)
            result.path = str(target)
            result.filename = target.name

            saved_meta = self._save_screenshot(image, target)
            result.saved = True
            result.file_size = int(saved_meta["file_size"])
            result.image_width = int(saved_meta["image_width"])
            result.image_height = int(saved_meta["image_height"])

            if copy_to_clipboard:
                self._copy_screenshot_to_clipboard(image)
                result.clipboard = True

            self._local_memory.record_file_action(target, action="screenshot_created", origin="pc_control.take_screenshot")
            self._local_memory.record_folder_snapshot(target.parent, origin="pc_control.take_screenshot")
            result.ok = True
            return result
        except Exception as exc:
            result.error = str(exc)
            return result

    def take_screenshot(
        self,
        save_path: str | None = None,
        copy_to_clipboard: bool = True,
    ) -> str:
        execution = self._execute_screenshot(save_path, copy_to_clipboard)
        if not execution.ok:
            logger.error(
                "screenshot_response_error requested_name=%s requested_dir=%s resolved_path=%s captured=%s saved=%s clipboard=%s error_message=%s",
                execution.requested_name,
                execution.requested_dir,
                execution.path,
                execution.captured,
                execution.saved,
                execution.clipboard,
                execution.error,
            )
            raise PCControlError(execution.error or "Falha ao tirar print.")

        logger.info(
            "screenshot_response_success requested_name=%s requested_dir=%s resolved_path=%s file_size=%s image_width=%s image_height=%s clipboard=%s",
            execution.requested_name,
            execution.requested_dir,
            execution.path,
            execution.file_size,
            execution.image_width,
            execution.image_height,
            execution.clipboard,
        )
        message = f"Print salvo em {execution.path}."
        if copy_to_clipboard and execution.clipboard:
            message += " Imagem copiada para o clipboard (Ctrl+V para colar)."
        return message

    def click_at(self, x: float | int, y: float | int) -> str:
        xi = int(round(float(x)))
        yi = int(round(float(y)))
        user32 = ctypes.windll.user32
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
        if xi < 0 or yi < 0 or xi >= width or yi >= height:
            raise PCControlError(f"Coordenadas fora da tela: ({xi}, {yi}) em {width}x{height}.")
        if not bool(user32.SetCursorPos(xi, yi)):
            raise PCControlError("Falha ao mover cursor para o clique.")
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        return f"Clique executado em ({xi}, {yi})."


def parse_duration_to_seconds(duration: str) -> int:
    cleaned = duration.strip().lower()
    if cleaned.isdigit():
        return int(cleaned)

    total = 0
    current = ""
    units = {
        "h": 3600,
        "hora": 3600,
        "horas": 3600,
        "m": 60,
        "min": 60,
        "mins": 60,
        "minuto": 60,
        "minutos": 60,
        "s": 1,
        "seg": 1,
        "segs": 1,
        "segundo": 1,
        "segundos": 1,
    }
    for token in cleaned.replace(",", " ").split():
        if token.isdigit():
            current = token
            continue

        digits = "".join(ch for ch in token if ch.isdigit())
        letters = "".join(ch for ch in token if ch.isalpha())
        if digits and letters and letters in units:
            total += int(digits) * units[letters]
            current = ""
            continue

        if current and token in units:
            total += int(current) * units[token]
            current = ""

    if total <= 0:
        raise PCControlError("Nao consegui entender a duracao informada.")
    return total


_BROWSER_CONTROLLER_SINGLETON: PCController | None = None


def _get_browser_controller() -> PCController:
    global _BROWSER_CONTROLLER_SINGLETON
    if _BROWSER_CONTROLLER_SINGLETON is None:
        _BROWSER_CONTROLLER_SINGLETON = PCController()
    return _BROWSER_CONTROLLER_SINGLETON


def detect_browser_command(text: str) -> str | None:
    """
    Detecta se texto contem comando de navegador.
    Retorna comando normalizado ou None.
    """
    text_lower = normalize_text(str(text or "")).strip()
    if not text_lower:
        return None

    allowed, reason = evaluate_browser_action(str(text or ""))
    if not allowed:
        logger.info("[PC_CONTROL] browser_command_blocked reason=%s text=%r", reason, str(text or "")[:120])
        return None

    if not any(word in text_lower for word in ["abre", "abrir", "abra", "navegue", "vai para", "acessa", "acessar"]):
        return None

    if is_browser_home_request(text_lower):
        logger.info("[PC_CONTROL] command_detected keyword=browser_home command=google")
        return "google"

    sites = {
        "youtube": "youtube",
        "google": "google",
        "gmail": "gmail",
        "facebook": "facebook",
        "instagram": "instagram",
        "twitter": "twitter",
        "netflix": "netflix",
        "spotify": "spotify",
    }

    for keyword, command in sites.items():
        if keyword in text_lower:
            logger.info("[PC_CONTROL] command_detected keyword=%s command=%s", keyword, command)
            return command

    if "pesquisa" in text_lower or "pesquisar" in text_lower:
        search_term = (
            text_lower.replace("pesquisa", "")
            .replace("pesquisar", "")
            .replace("no google", "")
            .strip()
        )
        if search_term:
            command = f"pesquisar {search_term}"
            logger.info("[PC_CONTROL] command_detected keyword=pesquisar command=%s", command)
            return command
        return "pesquisar"

    url_match = re.search(r"(https?://[^\s]+|www\.[^\s]+)", text or "")
    if url_match:
        cmd = url_match.group(1)
        logger.info("[PC_CONTROL] command_detected keyword=url command=%s", cmd)
        return cmd

    open_match = re.search(r"\babre(?:\s+o|\s+a)?\s+(.+)$", text_lower)
    if open_match:
        command = open_match.group(1).strip()
        if command:
            logger.info("[PC_CONTROL] command_detected keyword=abre command=%s", command)
            return command

    return None


def browser_route(command: str, user_input: str = "") -> tuple[bool, str]:
    """
    Executa comando de navegador.

    Returns:
        (executado: bool, mensagem_confirmacao: str)
    """
    normalized_command = normalize_text(str(command or "")).strip()
    if not normalized_command:
        return False, "Comando de navegador vazio."

    # HOTFIX 3.5 — Browser Agent novo dono do turno: aborta a rota legada em
    # silencio (sem abrir navegador externo e sem status "Abrindo ...").
    try:
        from agent.core.browser_turn_context import browser_agent_owns_turn
        if browser_agent_owns_turn():
            logger.info("PC_CONTROL_OPEN_WEB_BLOCKED_BY_BROWSER_AGENT command=%r", normalized_command)
            logger.info("BROWSER_EXTERNAL_NAV_BLOCKED_BY_AGENT_ROUTE command=%r", normalized_command)
            return True, None
    except Exception:
        pass

    effective_source = str(user_input or "").strip() or f"abre {normalized_command}"
    allowed, reason = evaluate_browser_action(effective_source)
    if not allowed:
        logger.warning("[PC_CONTROL] browser_route_blocked reason=%s text=%r", reason, effective_source[:160])
        return False, "Navegacao bloqueada por falta de intencao explicita."

    logger.info("browser_route_decision_started command=%r", normalized_command)
    controller = _get_browser_controller()

    try:
        effective_command = effective_source if effective_source else f"abre {normalized_command}"
        response = controller.open_app_or_link_from_text(effective_command)

        friendly = str(response or "").strip()
        if not friendly.lower().startswith("abrindo"):
            if normalized_command.startswith(("http://", "https://", "www.")):
                domain = normalized_command
                if "://" in domain:
                    domain = domain.split("://", 1)[1]
                domain = domain.split("/", 1)[0]
                friendly = f"Abrindo {domain}"
            else:
                friendly = f"Abrindo {normalized_command}"

        logger.info("[PC_CONTROL] action_completed_with_voice message=%r", friendly)
        return True, friendly
    except Exception as e:
        logger.error("[PC_CONTROL] browser_route_failed error=%s", e)
        return False, f"Erro ao abrir navegador: {e}"
