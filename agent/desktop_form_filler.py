from __future__ import annotations

import time
from typing import Any

from profile_store import ProfileStoreError, load_profile


DEFAULT_WRITE_INTERVAL = 0.02


class DesktopFormFillerError(Exception):
    pass


def _load_pyautogui() -> Any:
    try:
        import pyautogui
    except ImportError as exc:
        raise DesktopFormFillerError(
            "PyAutoGUI nao esta instalado. Rode 'python -m pip install -r requirements.txt'."
        ) from exc

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.15
    return pyautogui


def click_and_type(
    x: int,
    y: int,
    text: str,
    *,
    select_all_first: bool = False,
) -> None:
    pyautogui = _load_pyautogui()
    pyautogui.moveTo(x, y, duration=0.12)
    pyautogui.click(x, y)
    time.sleep(0.1)

    if select_all_first:
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.05)

    pyautogui.write(str(text), interval=DEFAULT_WRITE_INTERVAL)


def fill_fields_by_coordinates(
    field_coordinates: dict[str, list[int] | tuple[int, int]],
) -> dict[str, Any]:
    if not isinstance(field_coordinates, dict) or not field_coordinates:
        raise DesktopFormFillerError(
            "Para formulario desktop eu preciso de um mapa de coordenadas por campo."
        )

    try:
        profile = load_profile()
    except ProfileStoreError as exc:
        raise DesktopFormFillerError(str(exc)) from exc

    if not profile:
        raise DesktopFormFillerError("O perfil local do usuario esta vazio.")

    done: list[str] = []
    missing: list[str] = []

    for field_name, raw_position in field_coordinates.items():
        value = profile.get(field_name)
        if value in (None, ""):
            missing.append(field_name)
            continue

        if not isinstance(raw_position, (list, tuple)) or len(raw_position) != 2:
            raise DesktopFormFillerError(
                f"As coordenadas do campo '{field_name}' precisam estar no formato [x, y]."
            )

        x, y = raw_position
        click_and_type(int(x), int(y), str(value), select_all_first=True)
        done.append(field_name)

    return {
        "ok": True,
        "mode": "desktop",
        "filled": done,
        "missing": missing,
        "submitted": False,
    }
