"""Área de transferência e prints (2026-11-05): ler/copiar texto, print de uma janela (mesmo minimizada) ou da tela.

O print sempre vai para o copiar/colar e é salvo: onde o Chefe disser, ou em Documentos\\Kira\\Imagens.
Roda no processo do agente (mesmo PC): win32 direto, sem passar pelo backend.
"""

from __future__ import annotations

import asyncio
import ctypes
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from livekit.agents import function_tool

try:
    from runtime.desktop_windows import find_windows
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.desktop_windows import find_windows

HOME = Path.home()
PLACES = {"area de trabalho": HOME / "Desktop", "desktop": HOME / "Desktop", "downloads": HOME / "Downloads",
          "documentos": HOME / "Documents", "imagens": HOME / "Pictures"}
DEFAULT_DIR = HOME / "Documents" / "Kira" / "Imagens"


def _fold(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").lower())
    return " ".join("".join(c for c in raw if not unicodedata.combining(c)).split())


def target_dir(destino: str) -> Path:
    """'na área de trabalho' -> Desktop; caminho existente vale; nada -> pasta da Kira."""
    folded = _fold(destino)
    for key, path in PLACES.items():
        if key in folded:
            return path
    raw = Path(str(destino or "").strip().strip('"'))
    return raw if destino.strip() and raw.is_absolute() else DEFAULT_DIR


def read_clipboard() -> str:
    import win32clipboard  # type: ignore
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return "Texto copiado:\n" + str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT))[:4000]
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_HDROP):
            files = win32clipboard.GetClipboardData(win32clipboard.CF_HDROP)
            return "Arquivos copiados: " + ", ".join(str(f) for f in files[:20])
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_DIB):
            return "Tem uma imagem copiada (print ou foto)."
        return "A área de transferência está vazia."
    finally:
        win32clipboard.CloseClipboard()


def capture(alvo: str):
    """Imagem PIL da janela pedida (restaura sem roubar o foco se estiver minimizada) ou da tela inteira."""
    from PIL import ImageGrab
    folded = _fold(alvo)
    if not folded or folded in {"tela", "tela inteira", "tudo", "monitor"}:
        return ImageGrab.grab(all_screens=True), "a tela"
    windows = find_windows(alvo)
    if not windows:
        raise LookupError(f"não achei janela aberta de {alvo}")
    win = windows[0]
    import win32gui, win32ui  # type: ignore
    from PIL import Image
    user32 = ctypes.windll.user32
    minimized = bool(user32.IsIconic(win.hwnd))
    if minimized:
        user32.ShowWindow(win.hwnd, 4)  # SW_SHOWNOACTIVATE: aparece sem pegar o foco
        time.sleep(0.45)  # o app redesenha
    try:
        left, top, right, bottom = win32gui.GetWindowRect(win.hwnd)
        w, h = max(1, right - left), max(1, bottom - top)
        hdc = win32gui.GetWindowDC(win.hwnd)
        src = win32ui.CreateDCFromHandle(hdc)
        mem = src.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(src, w, h)
        mem.SelectObject(bmp)
        user32.PrintWindow(win.hwnd, mem.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT: pega mesmo coberta por outra janela
        info = bmp.GetInfo()
        image = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
        win32gui.DeleteObject(bmp.GetHandle()); mem.DeleteDC(); src.DeleteDC(); win32gui.ReleaseDC(win.hwnd, hdc)
    finally:
        if minimized:
            user32.ShowWindow(win.hwnd, 6)  # SW_MINIMIZE: volta como estava
    return image, win.title[:60] or alvo


TV_WORDS = {"tv", "televisao", "a tv", "tv da sala", "televisao da sala"}


async def tv_print(destino: str) -> str:
    """Print da TV LG (foto da tela pelo próprio webOS), salvo e posto no copiar/colar."""
    from services.clipboard_manager import copy_image
    try:
        from runtime.media_tools import _post_local
    except ImportError:  # pragma: no cover
        from agent.runtime.media_tools import _post_local
    r = await _post_local("/api/ui/casa", {"action": "print_tela", "device": "tv", "value": str(target_dir(destino))}, read_timeout=40.0)
    if not r.get("ok"):
        return f"Não consegui o print da TV: {r.get('message') or 'a TV não respondeu'}."
    copied = await asyncio.to_thread(lambda: copy_image(r["arquivo"]).ok)
    return f"Print da TV salvo em {r['arquivo']}" + (" e já está no copiar/colar (Ctrl+V)." if copied else "; não consegui pôr no copiar/colar.")


def take_print(alvo: str, destino: str) -> str:
    from services.clipboard_manager import copy_image
    image, name = capture(alvo)
    folder = target_dir(destino)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"print_{datetime.now():%Y-%m-%d_%H-%M-%S}.png"
    image.save(path)
    copied = copy_image(path).ok
    return (f"Print de {name} salvo em {path}" + (" e já está no copiar/colar (é só colar com Ctrl+V)." if copied else
            "; não consegui pôr no copiar/colar."))


class ClipboardToolsMixin:
    @function_tool(
        name="area_de_transferencia",
        description=(
            "Copiar/colar do Chefe e prints. acao: ler (o que ele copiou) | copiar (texto vai para o copiar/colar; use quando "
            "ele pedir algo pronto para colar: diga 'já está no seu copiar, é só colar') | print (alvo = nome da janela/programa, "
            "ex. 'VS Code', 'Chrome', 'tela' (o PC) ou 'tv' (foto do que a TV LG mostra); funciona com a janela minimizada). O print sempre vai para o copiar/colar e é "
            "salvo: destino = onde ele pediu ('área de trabalho', 'downloads' ou caminho); sem destino, Documentos\\Kira\\Imagens."
        ),
    )
    async def area_de_transferencia(self, acao: str, texto: str = "", alvo: str = "", destino: str = "") -> str:
        acao = _fold(acao)
        try:
            if acao == "ler":
                return await asyncio.to_thread(read_clipboard)
            if acao == "copiar":
                from services.clipboard_manager import copy_text
                result = await asyncio.to_thread(copy_text, texto)
                return "Copiado: já está no copiar/colar do Chefe." if result.ok else "Não consegui copiar."
            if acao == "print":
                if _fold(alvo) in TV_WORDS or _fold(alvo).startswith("tv"):
                    return await tv_print(destino)
                return await asyncio.to_thread(take_print, alvo, destino)
        except LookupError as exc:
            return f"Não deu: {exc}."
        except Exception as exc:  # noqa: BLE001 - win32 pode falhar de mil jeitos; a Kira só precisa saber que falhou
            return f"Não consegui ({type(exc).__name__})."
        return "Ação desconhecida: use ler, copiar ou print."
