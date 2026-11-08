from __future__ import annotations

from typing import Any

from profile_store import ProfileStoreError, load_profile


DEFAULT_DEBUG_URL = "http://127.0.0.1:9222"
FIELD_MAP: dict[str, str] = {
    "nome": "nome_completo",
    "nome completo": "nome_completo",
    "nome_preferido": "nome_preferido",
    "email": "email",
    "telefone": "telefone",
    "celular": "telefone",
    "cpf": "cpf",
    "cep": "cep",
    "rua": "rua",
    "endereco": "rua",
    "numero": "numero",
    "bairro": "bairro",
    "cidade": "cidade",
    "estado": "estado",
    "pais": "pais",
    "data de nascimento": "data_nascimento",
}


class WebFormFillerError(Exception):
    pass


def _load_playwright() -> Any:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise WebFormFillerError(
            "Playwright nao esta instalado. Rode 'python -m pip install -r requirements.txt' e depois 'playwright install'."
        ) from exc

    return sync_playwright, PlaywrightTimeoutError


def _normalize_requested_fields(requested_fields: list[str] | None) -> set[str]:
    if not requested_fields:
        return set()
    return {field.strip().lower() for field in requested_fields if field.strip()}


def _resolve_entries(requested_fields: list[str] | None) -> list[tuple[str, str]]:
    requested = _normalize_requested_fields(requested_fields)
    if not requested:
        return list(FIELD_MAP.items())

    entries: list[tuple[str, str]] = []
    seen_profile_keys: set[str] = set()
    for label_text, profile_key in FIELD_MAP.items():
        if label_text.lower() in requested or profile_key.lower() in requested:
            entries.append((label_text, profile_key))
            seen_profile_keys.add(profile_key.lower())

    for raw_field in requested:
        if raw_field not in seen_profile_keys:
            entries.append((raw_field, raw_field))
            seen_profile_keys.add(raw_field)

    return entries


def _resolve_page(browser: Any) -> Any:
    for context in reversed(browser.contexts):
        if context.pages:
            return context.pages[-1]
    raise WebFormFillerError(
        "Nao encontrei uma pagina aberta no navegador conectado. Abra o site desejado e tente de novo."
    )


def _selector_candidates(label_text: str, profile_key: str) -> list[str]:
    tokens = {label_text.lower(), profile_key.lower()}
    normalized_tokens = {token.replace(" ", "_") for token in tokens}
    all_tokens = sorted(tokens | normalized_tokens)

    selectors: list[str] = []
    for token in all_tokens:
        selectors.extend(
            [
                f'input[name*="{token}" i]',
                f'input[id*="{token}" i]',
                f'textarea[name*="{token}" i]',
                f'textarea[id*="{token}" i]',
                f'select[name*="{token}" i]',
                f'select[id*="{token}" i]',
                f'input[autocomplete*="{token}" i]',
            ]
        )
    return selectors


def _fill_locator(locator: Any, value: str) -> bool:
    try:
        locator.first.fill(value, timeout=1200)
        return True
    except Exception:
        return False


def _select_locator(locator: Any, value: str) -> bool:
    try:
        locator.first.select_option(label=value, timeout=1200)
        return True
    except Exception:
        pass

    try:
        locator.first.select_option(value=value, timeout=1200)
        return True
    except Exception:
        return False


def _try_fill(page: Any, label_text: str, profile_key: str, value: Any) -> bool:
    if value in (None, ""):
        return False

    text_value = str(value)
    locator_attempts = [
        page.get_by_label(label_text, exact=False),
        page.get_by_placeholder(label_text),
        page.get_by_role("textbox", name=label_text),
        page.get_by_role("combobox", name=label_text),
    ]

    for locator in locator_attempts:
        if _fill_locator(locator, text_value):
            return True
        if _select_locator(locator, text_value):
            return True

    for selector in _selector_candidates(label_text, profile_key):
        locator = page.locator(selector)
        if _fill_locator(locator, text_value):
            return True
        if _select_locator(locator, text_value):
            return True

    return False


def fill_current_page_form(
    *,
    debug_port_url: str = DEFAULT_DEBUG_URL,
    requested_fields: list[str] | None = None,
) -> dict[str, Any]:
    try:
        profile = load_profile()
    except ProfileStoreError as exc:
        raise WebFormFillerError(str(exc)) from exc

    if not profile:
        raise WebFormFillerError("O perfil local do usuario esta vazio.")

    sync_playwright, playwright_timeout_error = _load_playwright()
    entries = _resolve_entries(requested_fields)
    filled: list[str] = []
    missing: list[str] = []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(debug_port_url)
            page = _resolve_page(browser)

            for label_text, profile_key in entries:
                value = profile.get(profile_key)
                ok = _try_fill(page, label_text, profile_key, value)
                if ok:
                    filled.append(profile_key)
                else:
                    missing.append(profile_key)
    except playwright_timeout_error as exc:
        raise WebFormFillerError(
            "O navegador conectado demorou demais para responder. Confere se a pagina ainda esta aberta."
        ) from exc
    except Exception as exc:
        raise WebFormFillerError(
            "Nao consegui conectar ao navegador atual. Abra o Chrome ou Edge com '--remote-debugging-port=9222' e tente de novo."
        ) from exc

    return {
        "ok": True,
        "mode": "web",
        "filled": sorted(set(filled)),
        "missing_or_not_found": sorted(set(missing)),
        "submitted": False,
    }
