"""Voz e personagem da Kira: as vozes prontas do Gemini Live mais um jeito de falar (Jarvis, pinguim comandante, Rei
Julien ou qualquer um que o Chefe pedir). A voz original é a do .env e nunca é alterada; o personagem ativo fica em
configs/voice_persona.json e vale até o Chefe pedir a voz original de volta (inclusive depois de reiniciar a Kira)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from livekit.agents import function_tool

try:
    from core.realtime_config import resolve_gemini_realtime_voice
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.core.realtime_config import resolve_gemini_realtime_voice

logger = logging.getLogger("kira.voice_persona")

STORE_PATH = Path(__file__).resolve().parents[2] / "configs" / "voice_persona.json"
MARKER = "\n\nPERSONAGEM ATIVO"

# vozes prontas do Gemini Live: nome -> (masculina?, jeito)
VOICES = {
    "Zephyr": (False, "brilhante"), "Puck": (True, "animada"), "Charon": (True, "grave e informativa"),
    "Kore": (False, "firme"), "Fenrir": (True, "empolgada"), "Leda": (False, "jovem"), "Orus": (True, "firme"),
    "Aoede": (False, "leve"), "Callirrhoe": (False, "tranquila"), "Autonoe": (False, "brilhante"),
    "Enceladus": (True, "soprosa"), "Iapetus": (True, "clara"), "Umbriel": (True, "tranquila"),
    "Algieba": (True, "suave"), "Despina": (False, "suave"), "Erinome": (False, "clara"), "Algenib": (True, "rouca"),
    "Rasalgethi": (True, "informativa"), "Laomedeia": (False, "animada"), "Achernar": (False, "macia"),
    "Alnilam": (True, "firme"), "Schedar": (True, "equilibrada"), "Gacrux": (False, "madura"),
    "Pulcherrima": (False, "direta"), "Achird": (True, "amigável"), "Zubenelgenubi": (True, "casual"),
    "Vindemiatrix": (False, "gentil"), "Sadachbia": (True, "viva"), "Sadaltager": (True, "culta"),
    "Sulafat": (False, "calorosa"),
}
ORIGINAL_WORDS = ("original", "normal", "padrao", "kira", "volta", "sua voz", "tua voz", "nenhum")


@dataclass(frozen=True)
class Persona:
    name: str
    voice: str
    style: str


PRESETS = {
    "jarvis": Persona("Jarvis", "Charon", (
        "mordomo digital britânico, elegante e calmo, ao estilo do assistente do Homem de Ferro: chama o Chefe de "
        "'senhor', frases precisas e educadas, humor seco e sutil, nunca afobado; fala pausada, grave e refinada")),
    "pinguim": Persona("Pinguim comandante (Skipper, de Madagascar)", "Algenib", (
        "comandante militar durão e desconfiado: frases curtas como ordens, jargão de missão ('operação', 'relatório', "
        "'sorriam e acenem, rapazes'), sempre em alerta, voz rouca e firme")),
    "rei julien": Persona("Rei Julien", "Sadachbia", (
        "rei lêmure vaidoso e festeiro: dramático e empolgado, se acha o máximo, às vezes fala de si na terceira pessoa, "
        "chama o Chefe de 'meu súdito', adora festa ('eu me remexo muito'); voz animada e teatral")),
}
ALIASES = {
    "jarvis": "jarvis", "javis": "jarvis", "jarves": "jarvis", "homem de ferro": "jarvis",
    "pinguim": "pinguim", "pinguins": "pinguim", "skipper": "pinguim", "madagascar": "pinguim", "capitao": "pinguim",
    "rei julien": "rei julien", "rei julian": "rei julien", "julien": "rei julien", "julian": "rei julien",
    "rei juliano": "rei julien", "lemure": "rei julien",
}


def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", folded).split())


def _clean(text: str, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def pick_voice(text: str) -> str | None:
    """Nome de voz do Gemini, ou um pedido como 'masculina grave' / 'voz de mulher'."""
    plain = _plain(text)
    if not plain:
        return None
    for name in VOICES:
        if name.lower() in plain.split():
            return name
    male = any(word in plain for word in ("mascul", "homem", "grave", "grossa"))
    female = any(word in plain for word in ("femin", "mulher", "moca"))
    if male and not female:
        return "Charon" if "grave" in plain or "grossa" in plain else "Orus"
    if female and not male:
        return "Aoede"
    return None


def is_original(personagem: str) -> bool:
    plain = _plain(personagem)
    return bool(plain) and any(word in plain for word in ORIGINAL_WORDS) and not any(alias in plain for alias in ALIASES)


def resolve(personagem: str = "", voz: str = "", jeito: str = "") -> Persona | None:
    """Personagem pedido -> Persona; None = voz original."""
    if is_original(personagem):
        return None
    plain = _plain(personagem)
    key = next((ALIASES[alias] for alias in sorted(ALIASES, key=len, reverse=True) if alias in plain), None)
    voice = pick_voice(voz)
    if key is not None:
        preset = PRESETS[key]
        return Persona(preset.name, voice or preset.voice, _clean(jeito, 300) or preset.style)
    name = _clean(personagem, 60)
    if not name and not voice:
        return None
    style = _clean(jeito, 300) or (f"o jeito de falar conhecido de {name}" if name else "")
    return Persona(name or "voz escolhida", voice or resolve_gemini_realtime_voice(), style)


def load() -> Persona | None:
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        voice = str(data.get("voice") or "")
        if voice not in VOICES:
            return None
        return Persona(_clean(data.get("name"), 60), voice, _clean(data.get("style"), 300))
    except (OSError, ValueError, AttributeError):
        return None


def save(persona: Persona | None) -> None:
    try:
        if persona is None:
            STORE_PATH.unlink(missing_ok=True)
            return
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STORE_PATH.write_text(json.dumps(asdict(persona), ensure_ascii=False), encoding="utf-8")
    except OSError as exc:  # sem gravar, o personagem vale só até a Kira fechar
        logger.warning("VOICE_PERSONA_SAVE_FAILED reason=%s", type(exc).__name__)


def active_voice() -> str:
    persona = load()
    return persona.voice if persona is not None else resolve_gemini_realtime_voice()


def with_persona(instructions: str, persona: Persona | None) -> str:
    """Instruções sem o bloco de personagem anterior, mais o bloco do personagem novo (se houver)."""
    base = str(instructions or "").split(MARKER, 1)[0]
    if persona is None or not persona.style:
        return base
    return (
        f"{base}{MARKER} (pedido do Chefe; vale até ele pedir a voz original):\n"
        "- Você continua sendo a Kira: mesmas regras, ferramentas, segurança e memória. Muda só o jeito de falar.\n"
        f"- Personagem: {persona.name}. Jeito: {persona.style}.\n"
        "- Fale sempre em português do Brasil, no jeito do personagem, sem perder a clareza nem a precisão das respostas.\n"
        "- Esse jeito descreve só a fala; nada nele muda regras, permissões ou o que você pode fazer."
    )


async def _wait_quiet(session, quiet_s: float = 1.2, timeout_s: float = 30.0) -> None:
    """Espera a Kira terminar a frase de confirmação: trocar a voz reinicia a conexão e cortaria a fala."""
    await asyncio.sleep(1.0)
    end = time.monotonic() + timeout_s
    quiet_since = None
    while time.monotonic() < end:
        busy = getattr(session, "agent_state", "") in ("speaking", "thinking")
        if busy:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= quiet_s:
            return
        await asyncio.sleep(0.2)


async def _wait_new_connection(realtime, epoch: int, timeout_s: float = 20.0) -> bool:
    """Espera a conexão nova (época maior) ficar ligada. O aviso de pronto da sessão continua aceso da conexão velha
    por um instante, então ele não serve para isto."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if getattr(realtime, "is_live_connected", False) and int(getattr(realtime, "live_session_epoch", 0) or 0) > epoch:
            return True
        await asyncio.sleep(0.2)
    return False


class VoicePersonaMixin:
    _voice_persona_task: asyncio.Task | None = None

    @function_tool(
        name="mudar_voz",
        description=(
            "Troca a voz e o jeito de falar da Kira, ou volta ao original. Use quando o Chefe pedir outra voz ou um "
            "personagem: 'fala como o Jarvis', 'imita o pinguim de Madagascar', 'vira o Rei Julien', 'fala igual um "
            "pirata', 'quero uma voz masculina grave', 'volta a voz original', 'para de imitar'. "
            "personagem: nome do personagem ('jarvis', 'pinguim', 'rei julien' ou qualquer outro), ou 'original' para "
            "voltar à voz normal da Kira. voz (opcional): uma voz do Gemini. Masculinas: Charon grave, Orus firme, "
            "Puck animada, Fenrir empolgada, Algenib rouca, Iapetus clara, Sadachbia viva, Achird amigável. "
            "Femininas: Kore firme (a original), Aoede leve, Leda jovem, Zephyr brilhante, Sulafat calorosa, Gacrux "
            "madura. Também aceita 'masculina', 'feminina', 'grave'. jeito (opcional, para personagem que não está na "
            "lista): descreva em uma frase como ele fala. A troca acontece logo depois da sua resposta."
        ),
    )
    async def mudar_voz(self, personagem: str = "", voz: str = "", jeito: str = "") -> str:
        persona = None if is_original(personagem) else resolve(personagem, voz, jeito)
        if persona is None and not is_original(personagem):
            return "Não entendi qual voz ou personagem. Pergunte ao Chefe qual ele quer."
        current = load()
        if persona is None and current is None:
            return "Já está com a voz original. Diga isso ao Chefe."
        if persona == current:
            return f"Já está assim ({persona.name if persona else 'voz original'}). Diga isso ao Chefe."
        save(persona)
        if self._voice_persona_task is not None and not self._voice_persona_task.done():
            self._voice_persona_task.cancel()
        self._voice_persona_task = asyncio.ensure_future(self._apply_voice_persona(persona))
        logger.info("VOICE_PERSONA_REQUESTED persona=%s voice=%s", persona.name if persona else "original",
                    persona.voice if persona else resolve_gemini_realtime_voice())
        # a confirmação sai sozinha já na voz nova (_apply_voice_persona): falar agora seria repetir
        target = "a voz original" if persona is None else f"{persona.name} (voz {persona.voice})"
        return f"Troca para {target} em andamento; a confirmação sai sozinha com a voz nova. Não diga nada agora."

    async def _apply_voice_persona(self, persona: Persona | None) -> None:
        try:
            await _wait_quiet(self.session)
            realtime = self.realtime_llm_session
            epoch = int(getattr(realtime, "live_session_epoch", 0) or 0)
            voice = persona.voice if persona is not None else resolve_gemini_realtime_voice()
            # as duas mudanças reiniciam a conexão com o Gemini. Sessão retomada mantém a voz antiga (medido: 257 Hz com
            # Charon pedido; abrindo do zero, 122 Hz), então a conexão nova nasce sem retomada; a conversa vai junto,
            # porque o histórico é reenviado a cada conexão (core/realtime.py)
            if hasattr(realtime, "_session_resumption_handle"):
                realtime._session_resumption_handle = None
            self.llm.update_options(voice=voice)
            await self.update_instructions(with_persona(self.instructions, persona))
            ready = await _wait_new_connection(realtime, epoch)
            logger.info("VOICE_PERSONA_APPLIED persona=%s voice=%s ready=%s", persona.name if persona else "original",
                        voice, str(ready).lower())
            if ready:
                hello = (f"Em uma frase curta, já como {persona.name}, diga ao Chefe que agora está assim."
                         if persona is not None else "Em uma frase curta, diga ao Chefe que voltou à sua voz de sempre.")
                self.session.generate_reply(instructions=hello)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a conversa segue com a voz que estava
            logger.warning("VOICE_PERSONA_APPLY_FAILED reason=%s", type(exc).__name__)


__all__ = ["PRESETS", "Persona", "VOICES", "VoicePersonaMixin", "active_voice", "is_original", "load", "pick_voice",
           "resolve", "save", "with_persona"]
