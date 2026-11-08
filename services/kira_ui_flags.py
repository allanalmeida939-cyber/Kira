"""Pure helpers for selecting the desktop UI feature flag."""

from __future__ import annotations


_DISABLED_VALUES = frozenset({"0", "false", "off"})


def desktop_ui_query(env_value: str | None) -> str:
    """Return the desktop URL query for the UI flag.

    The desktop UI is enabled unless the environment explicitly contains one
    of the documented false values. Whitespace and case are ignored.
    """

    normalized = str(env_value or "").strip().lower()
    enabled = normalized not in _DISABLED_VALUES
    return f"?kiraui={'1' if enabled else '0'}&kira.desktop=1"
