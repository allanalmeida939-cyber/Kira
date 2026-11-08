from __future__ import annotations

import re
from dataclasses import dataclass

from .models import ConditionOutcome, VisualRuntimeSnapshot, VisualWatchPlan, normalize_text


LOADING_KEYWORDS = (
    "carregando",
    "loading",
    "aguarde",
    "abrindo",
    "processando",
    "sincronizando",
    "baixando",
    "download",
    "instalando",
    "instalacao",
    "updating",
    "atualizando",
)
ERROR_KEYWORDS = (
    "erro",
    "error",
    "falha",
    "failed",
    "nao foi possivel",
    "problema",
)
SUCCESS_KEYWORDS = (
    "concluido",
    "concluida",
    "feito",
    "sucesso",
    "success",
    "pronto",
    "completed",
    "done",
)

_QUOTED_TEXT_RE = re.compile(r"[\"'“”](.+?)[\"'“”]")
_BUTTON_TARGET_RE = re.compile(r"botao(?: de| do| da)? ([a-z0-9 _-]+)")
_WINDOW_TARGET_RE = re.compile(r"janela(?: de| do| da)? ([a-z0-9 _-]+)")
_TEXT_TARGET_RE = re.compile(r"(?:texto|mensagem)(?: de| do| da)? ([a-z0-9 _-]+)")
_OBJECT_TARGET_RE = re.compile(
    r"(?:aparecer|surgir|mostrar)(?:\s+(?:um|uma|o|a))?\s+([a-z0-9 _-]+?)(?:\s+na\s+tela|\s+no\s+jogo|\s+na\s+janela|$)"
)
_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")
_PROGRESS_FRACTION_RE = re.compile(r"\b(\d{1,3})\s*/\s*(\d{1,3})\b")


class VisualWatchError(Exception):
    pass


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _signature_diff_score(current: bytes, previous: bytes) -> float:
    if not current or not previous or len(current) != len(previous):
        return 0.0
    total = 0
    for left, right in zip(current, previous):
        total += abs(left - right)
    return total / max(len(current), 1)


def _extract_target_text(raw_text: str, normalized_text: str) -> str:
    quoted = _QUOTED_TEXT_RE.search(raw_text)
    if quoted:
        return normalize_text(quoted.group(1))

    for pattern in (_BUTTON_TARGET_RE, _WINDOW_TARGET_RE, _TEXT_TARGET_RE):
        match = pattern.search(normalized_text)
        if not match:
            continue
        target = normalize_text(match.group(1))
        target = re.split(
            r"\b(?:me avisa|assim que|quando|sumir|aparecer|ficar clicavel|ficar disponivel|carregar|terminar|concluir|abrir)\b",
            target,
            maxsplit=1,
        )[0].strip()
        if target:
            return target

    object_match = _OBJECT_TARGET_RE.search(normalized_text)
    if object_match:
        target = normalize_text(object_match.group(1))
        if target:
            return target

    return ""


def _default_success_message(kind: str, normalized_text: str, target_text: str) -> str:
    if kind == "screen_change":
        return "A tela mudou."
    if kind == "progress_complete":
        return "Chegou em 100%."
    if kind == "object_appears":
        return f'Apareceu "{target_text}".' if target_text else "O objeto apareceu na tela."
    if kind == "error_appears":
        return "Apareceu uma mensagem de erro."
    if kind == "success_appears":
        return "Apareceu uma mensagem de sucesso."
    if kind == "button_clickable":
        return f'O botao "{target_text}" ficou clicavel.' if target_text else "O botao ficou clicavel."
    if kind == "button_appears":
        return f'O botao "{target_text}" apareceu.' if target_text else "O botao apareceu."
    if kind == "text_appears":
        return f'Apareceu "{target_text}".' if target_text else "O texto apareceu."
    if kind == "text_disappears":
        return f'"{target_text}" sumiu.' if target_text else "O texto sumiu."
    if kind == "window_closes":
        return "A janela fechou."
    if kind == "window_opens":
        if "jogo" in normalized_text:
            return "O jogo abriu."
        if "app" in normalized_text or "aplicativo" in normalized_text:
            return "O app abriu."
        if target_text:
            return f'"{target_text}" abriu.'
        return "A janela abriu."
    if "baix" in normalized_text:
        return "Terminou de baixar."
    if "instal" in normalized_text:
        return "A instalacao terminou."
    if "site" in normalized_text and "carreg" in normalized_text:
        return "O site carregou."
    if "jogo" in normalized_text:
        return "O jogo abriu."
    if "app" in normalized_text or "aplicativo" in normalized_text:
        return "O app abriu."
    if "barra" in normalized_text and _contains_any(normalized_text, ("sumir", "desaparec")):
        return "A barra de carregamento sumiu."
    return "A condicao aconteceu."


def _default_confirmation_message(kind: str) -> str:
    if kind in {"completion", "progress_complete"}:
        return "Fechado, te aviso quando terminar."
    if kind == "object_appears":
        return "Fechado, vou ficar de olho na tela e te aviso quando aparecer."
    if kind in {"screen_change", "window_opens", "window_closes"}:
        return "Ta certo, fiquei de olho."
    return "Boa, vou monitorar isso."


def build_visual_watch_plan(request_text: str) -> VisualWatchPlan:
    raw_text = str(request_text or "").strip()
    normalized_text = normalize_text(raw_text)
    if not normalized_text:
        raise VisualWatchError("Me diz o que eu devo monitorar na tela.")

    target_text = _extract_target_text(raw_text, normalized_text)
    kind = "completion"
    requires_ui = False
    interval_seconds = 1.2
    timeout_seconds: float | None = 300.0
    stable_seconds = 3.0
    min_change_score = 6.5

    if "100%" in normalized_text or _contains_any(
        normalized_text,
        ("chegar em 100", "atingir 100", "porcentagem chegar"),
    ):
        kind = "progress_complete"
        requires_ui = True
    elif _contains_any(normalized_text, ("mensagem de erro", "erro aparecer")):
        kind = "error_appears"
        requires_ui = True
    elif _contains_any(normalized_text, ("mensagem de sucesso", "sucesso aparecer")):
        kind = "success_appears"
        requires_ui = True
    elif "botao" in normalized_text and _contains_any(
        normalized_text,
        ("clicavel", "habilitado", "disponivel"),
    ):
        kind = "button_clickable"
        requires_ui = True
    elif "botao" in normalized_text and _contains_any(
        normalized_text,
        ("aparecer", "surgir", "mostrar"),
    ):
        kind = "button_appears"
        requires_ui = True
    elif _contains_any(normalized_text, ("sumir", "desaparecer", "fechar")):
        if _contains_any(normalized_text, ("barra", "loading", "spinner", "carregando", "progresso", "%")):
            kind = "completion"
            requires_ui = True
        elif _contains_any(normalized_text, ("janela", "app", "aplicativo")):
            kind = "window_closes"
            requires_ui = bool(target_text)
        else:
            kind = "text_disappears"
            requires_ui = True
    elif _contains_any(normalized_text, ("aparecer", "surgir", "mostrar")):
        if target_text and any(token in normalized_text for token in ("na tela", "no jogo", "jogo", "minecraft")):
            kind = "object_appears"
            requires_ui = False
        else:
            kind = "text_appears"
            requires_ui = True
    elif _contains_any(normalized_text, ("tela mudar", "essa tela mudar", "mudar a tela")):
        kind = "screen_change"
        stable_seconds = 0.0
        timeout_seconds = 180.0
        min_change_score = 8.0
    elif _contains_any(normalized_text, ("abrir", "abrir esse", "abrir essa", "abrir o")):
        kind = "window_opens"
        requires_ui = bool(target_text)
        stable_seconds = 2.5
    elif _contains_any(
        normalized_text,
        (
            "carregar",
            "terminar",
            "concluir",
            "baixar",
            "instalar",
            "loading",
            "spinner",
            "barra de carregamento",
        ),
    ):
        kind = "completion"
        requires_ui = _contains_any(
            normalized_text,
            (
                "loading",
                "spinner",
                "barra",
                "porcentagem",
                "texto",
                "site",
                "download",
                "instalar",
            ),
        )

    description = {
        "completion": "Avisar quando o carregamento ou processo visual terminar",
        "screen_change": "Avisar quando a tela mudar de forma relevante",
        "window_opens": "Avisar quando uma janela ou app abrir",
        "window_closes": "Avisar quando a janela ou contexto sumir",
        "text_appears": "Avisar quando um texto ou elemento aparecer",
        "text_disappears": "Avisar quando um texto ou elemento sumir",
        "object_appears": "Avisar quando algo especifico aparecer na tela",
        "button_appears": "Avisar quando um botao aparecer",
        "button_clickable": "Avisar quando um botao ficar clicavel",
        "error_appears": "Avisar quando um erro aparecer",
        "success_appears": "Avisar quando um sucesso aparecer",
        "progress_complete": "Avisar quando a porcentagem chegar em 100%",
    }.get(kind, "Avisar quando a condicao visual acontecer")

    dedupe_key = normalize_text(f"{kind}|{target_text}|{normalized_text}")
    return VisualWatchPlan(
        kind=kind,
        request_text=raw_text,
        target_text=target_text,
        description=description,
        confirmation_message=_default_confirmation_message(kind),
        success_message=_default_success_message(kind, normalized_text, target_text),
        interval_seconds=interval_seconds,
        timeout_seconds=timeout_seconds,
        requires_ui=requires_ui,
        min_change_score=min_change_score,
        stable_seconds=stable_seconds,
        dedupe_key=dedupe_key,
    )


def _snapshot_strings(snapshot: VisualRuntimeSnapshot) -> tuple[str, ...]:
    values = [normalize_text(snapshot.window_title), normalize_text(snapshot.screen_summary)]
    if snapshot.ui is not None:
        values.extend(snapshot.ui.texts)
        values.extend(snapshot.ui.buttons)
        values.extend(snapshot.ui.enabled_buttons)
    return tuple(value for value in values if value)


def _snapshot_has_keyword(snapshot: VisualRuntimeSnapshot, keywords: tuple[str, ...]) -> bool:
    if snapshot.ui is not None:
        if keywords is LOADING_KEYWORDS and snapshot.ui.loading_visible:
            return True
        if keywords is ERROR_KEYWORDS and snapshot.ui.error_visible:
            return True
        if keywords is SUCCESS_KEYWORDS and snapshot.ui.success_visible:
            return True
    return any(any(keyword in value for keyword in keywords) for value in _snapshot_strings(snapshot))


def _extract_progress_values(snapshot: VisualRuntimeSnapshot) -> tuple[int, ...]:
    values = list(snapshot.ui.progress_values if snapshot.ui is not None else ())
    for value in _snapshot_strings(snapshot):
        for match in _PERCENT_RE.findall(value):
            try:
                percent = int(match)
            except ValueError:
                continue
            if 0 <= percent <= 100:
                values.append(percent)
        for left_str, right_str in _PROGRESS_FRACTION_RE.findall(value):
            try:
                left = int(left_str)
                right = int(right_str)
            except ValueError:
                continue
            if right <= 0:
                continue
            percent = int(round((left / right) * 100.0))
            if 0 <= percent <= 100:
                values.append(percent)
    return tuple(sorted(set(values)))


@dataclass(slots=True)
class ScreenChangeCondition:
    baseline_signature: bytes
    baseline_frame_token: int
    min_change_score: float

    def evaluate(self, snapshot: VisualRuntimeSnapshot) -> ConditionOutcome:
        if snapshot.frame_token <= self.baseline_frame_token:
            return ConditionOutcome()
        if not snapshot.signature or not self.baseline_signature:
            return ConditionOutcome()
        if _signature_diff_score(snapshot.signature, self.baseline_signature) >= self.min_change_score:
            return ConditionOutcome(triggered=True, reason="frame_changed")
        return ConditionOutcome()


@dataclass(slots=True)
class TextPresenceCondition:
    target_text: str
    should_disappear: bool = False
    button_only: bool = False
    enabled_only: bool = False
    seen_target: bool = False

    def evaluate(self, snapshot: VisualRuntimeSnapshot) -> ConditionOutcome:
        if self.button_only:
            present = snapshot.contains_button(self.target_text, enabled_only=self.enabled_only)
        else:
            present = snapshot.contains_text(self.target_text)

        if present:
            self.seen_target = True

        if self.should_disappear:
            if self.seen_target and not present:
                return ConditionOutcome(triggered=True, reason="target_disappeared")
            return ConditionOutcome()

        if present:
            return ConditionOutcome(triggered=True, reason="target_appeared")
        return ConditionOutcome()


@dataclass(slots=True)
class KeywordCondition:
    keywords: tuple[str, ...]

    def evaluate(self, snapshot: VisualRuntimeSnapshot) -> ConditionOutcome:
        if _snapshot_has_keyword(snapshot, self.keywords):
            return ConditionOutcome(triggered=True, reason="keyword_visible")
        return ConditionOutcome()


@dataclass(slots=True)
class ProgressCompleteCondition:
    seen_progress: bool = False
    consecutive_match_hits: int = 0
    min_consecutive_hits: int = 2

    def evaluate(self, snapshot: VisualRuntimeSnapshot) -> ConditionOutcome:
        values = _extract_progress_values(snapshot)
        observed_value = max(values) if values else None
        if values and max(values) < 100:
            self.seen_progress = True
            self.consecutive_match_hits = 0
        if values and max(values) >= 100:
            self.consecutive_match_hits += 1
            if self.consecutive_match_hits >= max(self.min_consecutive_hits, 1):
                return ConditionOutcome(triggered=True, reason=f"progress_100:{observed_value}")
            return ConditionOutcome(reason=f"progress_100_candidate:{observed_value}")
        self.consecutive_match_hits = 0
        if self.seen_progress and not _snapshot_has_keyword(snapshot, LOADING_KEYWORDS):
            return ConditionOutcome(triggered=True, reason="progress_finished")
        return ConditionOutcome()


@dataclass(slots=True)
class CompletionCondition:
    baseline_window_title: str
    baseline_frame_token: int
    min_change_score: float
    stable_seconds: float
    seen_activity: bool = False
    seen_loading: bool = False

    def evaluate(self, snapshot: VisualRuntimeSnapshot) -> ConditionOutcome:
        if snapshot.frame_token <= self.baseline_frame_token:
            return ConditionOutcome()

        if snapshot.change_score >= self.min_change_score:
            self.seen_activity = True

        if _snapshot_has_keyword(snapshot, LOADING_KEYWORDS):
            self.seen_loading = True

        progress_values = _extract_progress_values(snapshot)
        if progress_values and max(progress_values) >= 100:
            return ConditionOutcome(triggered=True, reason="progress_100")

        if _snapshot_has_keyword(snapshot, SUCCESS_KEYWORDS):
            return ConditionOutcome(triggered=True, reason="success_keyword")

        if self.seen_loading and not _snapshot_has_keyword(snapshot, LOADING_KEYWORDS):
            if snapshot.stable_for_seconds >= max(self.stable_seconds, 1.0):
                return ConditionOutcome(triggered=True, reason="loading_finished")

        title_changed = normalize_text(snapshot.window_title) not in {"", self.baseline_window_title}
        if title_changed:
            self.seen_activity = True
        if self.seen_activity and snapshot.stable_for_seconds >= max(self.stable_seconds, 1.0):
            if title_changed or not _snapshot_has_keyword(snapshot, LOADING_KEYWORDS):
                return ConditionOutcome(triggered=True, reason="visual_settled")

        return ConditionOutcome()


@dataclass(slots=True)
class WindowOpenCondition:
    baseline_window_title: str
    target_text: str = ""
    seen_activity: bool = False
    min_change_score: float = 6.0
    stable_seconds: float = 2.5

    def evaluate(self, snapshot: VisualRuntimeSnapshot) -> ConditionOutcome:
        if snapshot.change_score >= self.min_change_score:
            self.seen_activity = True

        normalized_title = normalize_text(snapshot.window_title)
        if normalized_title and normalized_title != self.baseline_window_title:
            self.seen_activity = True

        if self.target_text:
            if snapshot.contains_text(self.target_text):
                return ConditionOutcome(triggered=True, reason="target_window_visible")
            return ConditionOutcome()

        if (
            self.seen_activity
            and normalized_title
            and normalized_title != self.baseline_window_title
            and snapshot.stable_for_seconds >= self.stable_seconds
        ):
            return ConditionOutcome(triggered=True, reason="window_changed")
        return ConditionOutcome()


def build_condition(plan: VisualWatchPlan, baseline: VisualRuntimeSnapshot):
    baseline_title = normalize_text(baseline.window_title)

    if plan.kind == "screen_change":
        return ScreenChangeCondition(
            baseline_signature=baseline.signature,
            baseline_frame_token=baseline.frame_token,
            min_change_score=plan.min_change_score,
        )
    if plan.kind == "text_appears":
        return TextPresenceCondition(target_text=plan.target_text or plan.request_text)
    if plan.kind == "text_disappears":
        target = plan.target_text or plan.request_text
        return TextPresenceCondition(
            target_text=target,
            should_disappear=True,
            seen_target=baseline.contains_text(target),
        )
    if plan.kind == "button_appears":
        return TextPresenceCondition(
            target_text=plan.target_text or plan.request_text,
            button_only=True,
        )
    if plan.kind == "button_clickable":
        return TextPresenceCondition(
            target_text=plan.target_text or plan.request_text,
            button_only=True,
            enabled_only=True,
        )
    if plan.kind == "error_appears":
        return KeywordCondition(ERROR_KEYWORDS)
    if plan.kind == "success_appears":
        return KeywordCondition(SUCCESS_KEYWORDS)
    if plan.kind == "progress_complete":
        return ProgressCompleteCondition(
            seen_progress=bool(_extract_progress_values(baseline)),
        )
    if plan.kind == "window_closes":
        initial_target = plan.target_text or baseline.window_title or plan.request_text
        return TextPresenceCondition(
            target_text=initial_target,
            should_disappear=True,
            seen_target=baseline.contains_text(initial_target),
        )
    if plan.kind == "window_opens":
        return WindowOpenCondition(
            baseline_window_title=baseline_title,
            target_text=plan.target_text,
            min_change_score=plan.min_change_score,
            stable_seconds=max(plan.stable_seconds, 1.5),
        )
    return CompletionCondition(
        baseline_window_title=baseline_title,
        baseline_frame_token=baseline.frame_token,
        min_change_score=plan.min_change_score,
        stable_seconds=max(plan.stable_seconds, 1.0),
        seen_loading=_snapshot_has_keyword(baseline, LOADING_KEYWORDS),
    )
