from __future__ import annotations

import time
from collections import deque

from vision.observer import ObservationUpdate


class CommentaryEngine:
    def __init__(
        self,
        *,
        enabled: bool = True,
        base_cooldown: float = 18.0,
        repeat_guard: float = 40.0,
        user_grace_period: float = 4.0,
    ) -> None:
        self._enabled = bool(enabled)
        self._base_cooldown = base_cooldown
        self._repeat_guard = repeat_guard
        self._user_grace_period = user_grace_period
        self._last_comment_at = 0.0
        self._last_signature = ""
        self._topic_started_at: dict[str, float] = {}
        self._last_topic_comment_at: dict[str, float] = {}
        self._game_failure_times: deque[float] = deque(maxlen=6)

    def build_comment(
        self,
        update: ObservationUpdate,
        *,
        visual_snapshot=None,
        now: float | None = None,
    ) -> str | None:
        now = now or time.monotonic()
        state = update.state
        if not self._enabled:
            return None
        if not update.notable or not getattr(state, "visual_context_active", False):
            return None

        if now - getattr(state, "last_user_activity_at", 0.0) < self._user_grace_period:
            return None

        comment, topic = self._comment_for(update, visual_snapshot=visual_snapshot, now=now)
        if not comment:
            return None

        cooldown = self._cooldown_for(state, topic)
        if now - self._last_comment_at < cooldown:
            return None

        signature = self._signature_for(update, topic)
        repeat_guard = self._repeat_guard_for(state, topic)
        if signature == self._last_signature and now - self._last_comment_at < repeat_guard:
            return None
        if topic and now - self._last_topic_comment_at.get(topic, 0.0) < repeat_guard:
            return None

        self._last_signature = signature
        self._last_comment_at = now
        if topic:
            self._last_topic_comment_at[topic] = now
        return comment

    def _comment_for(self, update: ObservationUpdate, *, visual_snapshot, now: float) -> tuple[str | None, str]:
        state = update.state
        domain = str(getattr(state, "domain", "") or "desktop")
        app_name = str(getattr(state, "app_name", "") or "").strip()
        ui_mode = str(getattr(state, "ui_mode", "") or "").strip()
        ui_summary = str(getattr(state, "ui_summary", "") or "").strip()
        ui_primary_action = str(getattr(state, "ui_primary_action", "") or "").strip()
        ui_prominent_text = str(getattr(state, "ui_prominent_text", "") or "").strip()
        action_guess = str(getattr(state, "action_guess", "") or "").strip()
        change_score = float(getattr(state, "visual_change_score", 0.0) or 0.0)
        if visual_snapshot is not None:
            change_score = max(change_score, float(getattr(visual_snapshot, "change_score", 0.0) or 0.0))

        topic_prefix = app_name or domain or "desktop"

        if update.event_kind == "visual_context_started":
            return None, ""

        loading_duration = self._topic_duration(
            f"loading:{topic_prefix}",
            active=ui_mode == "loading",
            now=now,
        )
        error_duration = self._topic_duration(
            f"error:{topic_prefix}:{ui_prominent_text or ui_summary}",
            active=ui_mode == "erro" or action_guess == "erro_visivel",
            now=now,
        )

        if ui_mode == "loading" and loading_duration >= 18.0:
            if domain == "game":
                return (
                    "Esse loading ta demorando mais do que o charme dele justifica.",
                    f"{topic_prefix}:loading_slow",
                )
            return (
                "Esse loading ja ta pedindo paciencia premium.",
                f"{topic_prefix}:loading_slow",
            )

        if ui_mode == "erro" or action_guess == "erro_visivel":
            if error_duration >= 8.0 or update.event_kind in {"app_changed", "window_focus_changed", "screen_scene_changed"}:
                if domain == "code":
                    return (
                        "Hmm... isso ta com cara de erro visivel. Se quiser, eu te ajudo a destrinchar.",
                        f"{topic_prefix}:error",
                    )
                return (
                    "Tem erro visivel ai. Se quiser, eu te ajudo a entender rapido.",
                    f"{topic_prefix}:error",
                )

        if domain == "game":
            comment = self._comment_for_game(
                app_name=app_name,
                ui_mode=ui_mode,
                ui_summary=ui_summary,
                ui_primary_action=ui_primary_action,
                change_score=change_score,
                update_event=update.event_kind,
                now=now,
            )
            if comment:
                return comment

        if ui_primary_action and any(
            token in ui_primary_action for token in ("jogar", "continuar", "instalar", "baixar", "entrar", "enviar")
        ):
            if update.event_kind in {"app_changed", "window_focus_changed", "screen_scene_changed", "domain_changed"}:
                return (
                    f'Apareceu um botao importante ai: "{ui_primary_action}".',
                    f"{topic_prefix}:primary_action:{ui_primary_action}",
                )

        if domain == "code":
            if ui_mode == "dialogo":
                return (
                    "Tem um dialogo ai na frente do codigo. Se quiser, eu te ajudo a decidir rapido.",
                    f"{topic_prefix}:code_dialog",
                )
            if update.event_kind in {"app_detected", "app_changed"}:
                return (
                    "Isso ta com cara de codigo. Se aparecer erro, traceback ou build quebrando, eu entro na hora.",
                    f"{topic_prefix}:code_context",
                )

        if domain == "web":
            if ui_mode == "login":
                return (
                    "Isso parece uma tela de login. Se quiser, eu te ajudo a ler ou preencher rapidinho.",
                    f"{topic_prefix}:web_login",
                )
            if ui_mode == "formulario":
                return (
                    "Isso ta com cara de formulario. Posso te ajudar a revisar antes de enviar.",
                    f"{topic_prefix}:web_form",
                )
            if ui_mode == "chat":
                return (
                    "Parece um chat aberto. Se quiser responder com base no que ta ai, eu acompanho contigo.",
                    f"{topic_prefix}:web_chat",
                )
            if ui_mode in {"painel", "menu", "dialogo"}:
                return (
                    "Essa interface ta bem de painel/menu. Se tu quiser, eu te ajudo a achar o caminho mais rapido.",
                    f"{topic_prefix}:web_panel",
                )
            if update.event_kind in {"app_detected", "app_changed", "domain_changed"}:
                return (
                    "Parece navegacao web. Vou ficar de olho no que for realmente util sem virar locutora.",
                    f"{topic_prefix}:web_context",
                )

        if domain == "document":
            if update.event_kind in {"app_detected", "app_changed"}:
                return (
                    "Isso parece um documento ou planilha. Se pintar algo estranho ou importante, eu chamo.",
                    f"{topic_prefix}:document_context",
                )

        if domain == "video":
            if update.event_kind in {"app_detected", "app_changed"}:
                return (
                    "Parece um video ou tutorial. Se surgir algo util ou confuso, eu comento.",
                    f"{topic_prefix}:video_context",
                )

        if ui_summary and update.event_kind in {"screen_scene_changed", "window_focus_changed"} and change_score >= 10.0:
            return (
                f"Teve uma mudanca visual relevante ai. {ui_summary[:90]}.",
                f"{topic_prefix}:scene:{ui_mode or 'generic'}",
            )

        if update.event_kind == "screen_scene_changed" and domain == "desktop" and change_score >= 12.0:
            return (
                "Opa, teve uma mudanca boa nessa tela ai.",
                f"{topic_prefix}:scene_desktop",
            )

        return None, ""

    def _comment_for_game(
        self,
        *,
        app_name: str,
        ui_mode: str,
        ui_summary: str,
        ui_primary_action: str,
        change_score: float,
        update_event: str,
        now: float,
    ) -> tuple[str | None, str]:
        topic_prefix = app_name or "jogo"

        if ui_mode == "derrota":
            self._remember_game_failure(now)
            if len(self._game_failure_times) >= 2:
                return (
                    "Essa parte ta judiando de voce, hein. Se quiser, eu penso numa saida curta contigo.",
                    f"{topic_prefix}:game_repeat_fail",
                )
            return "Ai foi de base. Bora de novo.", f"{topic_prefix}:game_defeat"

        if ui_mode == "vitoria":
            return "Ai sim. Isso teve cara de vitoria limpa.", f"{topic_prefix}:game_victory"

        if ui_mode == "recompensa":
            return "Boa. Pintou recompensa importante ai.", f"{topic_prefix}:game_reward"

        if ui_mode == "boss":
            return "Opa. Isso ta com cara de boss ou encontro grande.", f"{topic_prefix}:game_boss"

        if ui_mode == "perigo":
            return "Tem cara de perigo ai. Vale segurar um pouco e ler a sala.", f"{topic_prefix}:game_danger"

        if ui_mode == "inventario":
            return "Isso parece inventario. Da pra pensar build, item ou recurso rapido daqui.", f"{topic_prefix}:game_inventory"

        if ui_mode == "mapa":
            return "Mapa na tela. Se tu tiver meio perdido, agora da pra se orientar melhor.", f"{topic_prefix}:game_map"

        if ui_mode == "crafting":
            return "Isso ta com cara de crafting. Vale revisar material antes de gastar a toa.", f"{topic_prefix}:game_crafting"

        if ui_mode == "menu" and ui_primary_action:
            return (
                f'Menu aberto. O caminho mais forte que eu to vendo ai e "{ui_primary_action}".',
                f"{topic_prefix}:game_menu",
            )

        if ui_mode == "combate" and update_event == "screen_scene_changed" and change_score >= 12.0:
            return "Eita. Isso ficou bem mais intenso agora.", f"{topic_prefix}:game_combat"

        if update_event in {"app_detected", "app_changed"}:
            if app_name:
                return (
                    f"Parece {app_name}. Vou acompanhar teu jogo com moderacao e entrar so nas horas boas.",
                    f"{topic_prefix}:game_context",
                )
            return (
                "Isso parece um jogo. Vou acompanhar as mudancas importantes sem narrar tudo.",
                f"{topic_prefix}:game_context",
            )

        if ui_summary and update_event == "screen_scene_changed" and change_score >= 10.0:
            return (
                f"Teve mudanca forte no jogo. {ui_summary[:90]}.",
                f"{topic_prefix}:game_scene",
            )

        return None, ""

    def _cooldown_for(self, state, topic: str) -> float:
        domain = str(getattr(state, "domain", "") or "desktop")
        domain_cooldowns = {
            "game": 11.0,
            "code": 18.0,
            "web": 22.0,
            "document": 24.0,
            "video": 24.0,
            "desktop": self._base_cooldown,
        }
        cooldown = domain_cooldowns.get(domain, self._base_cooldown)
        if any(token in topic for token in ("error", "danger", "victory", "defeat", "boss")):
            return min(cooldown, 9.0)
        if "loading_slow" in topic:
            return max(cooldown, 24.0)
        return cooldown

    def _repeat_guard_for(self, state, topic: str) -> float:
        domain = str(getattr(state, "domain", "") or "desktop")
        repeat_guard = 55.0 if domain == "game" else self._repeat_guard
        if "loading_slow" in topic:
            return max(repeat_guard, 45.0)
        if any(token in topic for token in ("web_context", "code_context", "document_context", "video_context")):
            return max(repeat_guard, 60.0)
        return repeat_guard

    def _signature_for(self, update: ObservationUpdate, topic: str) -> str:
        state = update.state
        ui_mode = str(getattr(state, "ui_mode", "") or "")
        ui_primary_action = str(getattr(state, "ui_primary_action", "") or "")
        return "|".join(
            [
                topic or update.event_kind,
                str(getattr(state, "domain", "") or ""),
                str(getattr(state, "app_name", "") or ""),
                str(getattr(state, "action_guess", "") or ""),
                ui_mode,
                ui_primary_action,
            ]
        )

    def _topic_duration(self, key: str, *, active: bool, now: float) -> float:
        if not key:
            return 0.0
        if active:
            started_at = self._topic_started_at.setdefault(key, now)
            return max(now - started_at, 0.0)
        self._topic_started_at.pop(key, None)
        return 0.0

    def _remember_game_failure(self, now: float) -> None:
        self._game_failure_times.append(now)
        cutoff = now - 180.0
        while self._game_failure_times and self._game_failure_times[0] < cutoff:
            self._game_failure_times.popleft()
