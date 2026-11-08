"""Guarda segredos do usuário (tokens OAuth, dados de saúde) criptografados com a DPAPI do Windows.

Só o mesmo usuário do Windows, na mesma máquina, consegue abrir. Os arquivos ficam em configs/secure/ (fora do git).
Fora do Windows não há cofre: nada é gravado e o recurso fica indisponível.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

SECURE_DIR = Path(__file__).resolve().parents[1] / "configs" / "secure"


class SecureStoreUnavailable(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise SecureStoreUnavailable("cofre do sistema indisponível")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    target = _Blob()
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not call(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(target)):
        raise SecureStoreUnavailable("falha no cofre do sistema")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


class SecureStore:
    def __init__(self, directory: Path = SECURE_DIR, *, protect=None, unprotect=None) -> None:
        self._dir = directory
        self._protect = protect or (lambda data: _dpapi(data, protect=True))
        self._unprotect = unprotect or (lambda data: _dpapi(data, protect=False))

    def _path(self, name: str) -> Path:
        if not name.replace("_", "").isalnum():
            raise ValueError("nome_invalido")
        return self._dir / f"{name}.bin"

    def save(self, name: str, value: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = self._protect(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        handle, temp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.replace(temp, self._path(name))

    def load(self, name: str) -> dict[str, Any] | None:
        try:
            raw = self._path(name).read_bytes()
        except OSError:
            return None
        try:
            value = json.loads(self._unprotect(raw).decode("utf-8"))
        except (SecureStoreUnavailable, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def delete(self, name: str) -> bool:
        try:
            self._path(name).unlink()
            return True
        except FileNotFoundError:
            return False


__all__ = ["SecureStore", "SecureStoreUnavailable", "SECURE_DIR"]
