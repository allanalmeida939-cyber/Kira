from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROFILE_PATH = Path(__file__).resolve().parent / "user_profile.json"


class ProfileStoreError(Exception):
    pass


def load_profile() -> dict[str, Any]:
    if not PROFILE_PATH.exists():
        return {}

    try:
        with PROFILE_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as exc:
        raise ProfileStoreError("O arquivo user_profile.json esta invalido.") from exc
    except OSError as exc:
        raise ProfileStoreError("Nao consegui ler o perfil local do usuario.") from exc

    if not isinstance(data, dict):
        raise ProfileStoreError("O perfil local precisa ser um objeto JSON.")
    return data


def save_profile(data: dict[str, Any]) -> None:
    try:
        with PROFILE_PATH.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise ProfileStoreError("Nao consegui salvar o perfil local do usuario.") from exc


def get_user_profile_field(field_name: str) -> Any:
    data = load_profile()
    return data.get(field_name)


def update_user_profile(field_name: str, value: Any) -> bool:
    data = load_profile()
    data[field_name] = value
    save_profile(data)
    return True
