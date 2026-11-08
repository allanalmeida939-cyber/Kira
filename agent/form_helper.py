from __future__ import annotations

from typing import Any

from profile_store import load_profile


def fill_form_from_profile(fields: list[str]) -> dict[str, Any]:
    profile = load_profile()
    result: dict[str, Any] = {}

    for field in fields:
        result[field] = profile.get(field)

    return result
