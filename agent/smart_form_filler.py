from __future__ import annotations

from typing import Any

from desktop_form_filler import DesktopFormFillerError, fill_fields_by_coordinates
from web_form_filler import DEFAULT_DEBUG_URL, WebFormFillerError, fill_current_page_form


BROWSER_KEYWORDS = ["chrome", "edge", "firefox", "opera", "brave"]


class SmartFormFillerError(Exception):
    pass


def _get_active_window_title_via_pygetwindow() -> str:
    try:
        import pygetwindow as gw
    except ImportError as exc:
        raise SmartFormFillerError(
            "PyGetWindow nao esta instalado. Rode 'python -m pip install -r requirements.txt'."
        ) from exc

    try:
        window = gw.getActiveWindow()
    except Exception as exc:
        raise SmartFormFillerError(
            "Nao consegui identificar a janela ativa do Windows."
        ) from exc

    if not window:
        return ""

    title = getattr(window, "title", "") or ""
    return title.strip().lower()


def get_active_window_title() -> str:
    return _get_active_window_title_via_pygetwindow()


def fill_form_smart(
    *,
    desktop_coordinates: dict[str, list[int] | tuple[int, int]] | None = None,
    requested_fields: list[str] | None = None,
    debug_port_url: str = DEFAULT_DEBUG_URL,
) -> dict[str, Any]:
    title = get_active_window_title()
    is_browser = any(keyword in title for keyword in BROWSER_KEYWORDS)

    if is_browser:
        try:
            result = fill_current_page_form(
                debug_port_url=debug_port_url,
                requested_fields=requested_fields,
            )
        except WebFormFillerError as exc:
            raise SmartFormFillerError(str(exc)) from exc
        result["active_window"] = title
        return result

    if desktop_coordinates:
        try:
            result = fill_fields_by_coordinates(desktop_coordinates)
        except DesktopFormFillerError as exc:
            raise SmartFormFillerError(str(exc)) from exc
        result["active_window"] = title
        return result

    raise SmartFormFillerError(
        "Nao consegui determinar um modo seguro para preencher esse formulario. "
        "Se for web, abra o navegador com depuracao remota. "
        "Se for app desktop, me passe um mapa de coordenadas por campo."
    )
