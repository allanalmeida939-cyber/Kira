from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import io
import subprocess
import tempfile


@dataclass
class ClipboardResult:
    ok: bool
    kind: str
    error: str | None = None
    preview: str | None = None
    path: str | None = None


def _copy_text_tk(text: str) -> ClipboardResult:
    try:
        import tkinter  # type: ignore

        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return ClipboardResult(ok=True, kind="text", preview=text[:120])
    except Exception as exc:
        return ClipboardResult(ok=False, kind="text", error=f"tkinter_failed:{exc}")


def _copy_text_powershell(text: str) -> ClipboardResult:
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value @'\n" + text + "\n'@"],
            check=True,
            capture_output=True,
            text=True,
        )
        return ClipboardResult(ok=True, kind="text", preview=text[:120])
    except Exception as exc:
        return ClipboardResult(ok=False, kind="text", error=f"powershell_failed:{exc}")


def copy_text(text: str) -> ClipboardResult:
    payload = str(text or "")
    try:
        import pyperclip  # type: ignore

        pyperclip.copy(payload)
        return ClipboardResult(ok=True, kind="text", preview=payload[:120])
    except Exception:
        pass
    res = _copy_text_tk(payload)
    if res.ok:
        return res
    return _copy_text_powershell(payload)


def copy_image(image_path: str | Path) -> ClipboardResult:
    p = Path(image_path)
    if not p.exists():
        return ClipboardResult(ok=False, kind="image", error="file_not_found", path=str(p))
    try:
        from PIL import Image  # type: ignore
        import win32clipboard  # type: ignore
    except Exception as exc:
        return ClipboardResult(ok=False, kind="image", error=f"missing_deps:{exc}", path=str(p))
    try:
        img = Image.open(p).convert("RGB")
        out = io.BytesIO()
        img.save(out, format="BMP")
        data = out.getvalue()[14:]  # CF_DIB expects BMP without file header
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
        finally:
            win32clipboard.CloseClipboard()
        return ClipboardResult(ok=True, kind="image", path=str(p))
    except Exception as exc:
        return ClipboardResult(ok=False, kind="image", error=f"copy_image_failed:{exc}", path=str(p))


def copy_file_path(path: str | Path) -> ClipboardResult:
    p = str(Path(path))
    return copy_text(p)


def get_clipboard_capabilities() -> dict:
    caps = {
        "text": {"pyperclip": False, "tkinter": False, "powershell": True},
        "image": {"pillow": False, "win32clipboard": False},
    }
    try:
        import pyperclip  # type: ignore # noqa: F401
        caps["text"]["pyperclip"] = True
    except Exception:
        pass
    try:
        import tkinter  # type: ignore # noqa: F401
        caps["text"]["tkinter"] = True
    except Exception:
        pass
    try:
        from PIL import Image  # type: ignore # noqa: F401
        caps["image"]["pillow"] = True
    except Exception:
        pass
    try:
        import win32clipboard  # type: ignore # noqa: F401
        caps["image"]["win32clipboard"] = True
    except Exception:
        pass
    return caps

