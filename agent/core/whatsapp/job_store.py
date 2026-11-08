"""job_store.py — a "gaveta de fichas" do WhatsApp V9.

Substitui a variavel `self._pending_whatsapp_confirmation` (e os dataclasses
PendingWhatsAppConfirmation/PendingWhatsAppSend) do assistant.py por um unico
WhatsappJob com maquina de estados explicita. Regra de ouro: o job existe ou nao
existe — nunca mais "ja mandei mas ainda tem pendente".

Sem dependencias do projeto (so stdlib) -> base segura de tudo.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4

# --- estados de entrega (delivery_state) -----------------------------------
CREATED = "created"
WAITING_CONTACT = "waiting_contact"          # contato nao encontrado; aguardando o nome certo
WAITING_CONFIRMATION = "waiting_confirmation"
SENDING = "sending"
SENT = "sent"
FAILED = "failed"

# estados em que o job ainda esta "vivo" (conta como pendente p/ rotear intent)
_ACTIVE_STATES = frozenset({CREATED, WAITING_CONTACT, WAITING_CONFIRMATION, SENDING})

# --- estados de fala (speech_state) ----------------------------------------
SILENT = "silent"
PRE_ACK = "pre_ack"
POST_ACK = "post_ack"

# --- modos -----------------------------------------------------------------
CONFIRM_REQUIRED = "required"
CONFIRM_SKIP = "skip"
PREVIEW_SHOW = "show"
PREVIEW_SKIP = "skip"


@dataclass(slots=True)
class WhatsappJob:
    """Uma ficha de envio de WhatsApp. Carrega tudo que o fluxo precisa."""

    id: str
    contact: str                              # como o usuario falou (cru)
    message: str                              # mensagem final a enviar
    confirmation_mode: str = CONFIRM_REQUIRED  # "required" | "skip"
    preview_mode: str = PREVIEW_SHOW           # "show" | "skip"
    delivery_state: str = CREATED              # created|waiting_confirmation|sending|sent|failed
    speech_state: str = SILENT                 # silent|pre_ack|post_ack
    # resolucao do contato (preenchido pelo orchestrator)
    resolved_contact: str = ""                 # nome de exibicao resolvido
    resolved_number: str = ""                  # numero/jid pro bridge
    message_id: str = ""
    context_item_id: str | None = None
    first_person: bool = False                 # mensagem escrita "como se fosse o usuario"
    error: str = ""
    created_at: float = field(default_factory=time.time)


class WhatsappJobStore:
    """Guarda no maximo UM job ativo por vez (a sessao do worker e single-user).

    Thread-safe via RLock. Metodos de classe para uso global simples
    (`WhatsappJobStore.has_pending()` no agent.py, etc.).
    """

    _current: WhatsappJob | None = None
    _lock = threading.RLock()

    @classmethod
    def create(
        cls,
        *,
        contact: str,
        message: str,
        confirmation_mode: str = CONFIRM_REQUIRED,
        preview_mode: str = PREVIEW_SHOW,
        resolved_contact: str = "",
        resolved_number: str = "",
        message_id: str = "",
        context_item_id: str | None = None,
        first_person: bool = False,
    ) -> WhatsappJob:
        """Cria e registra um job, substituindo qualquer pendente anterior."""
        with cls._lock:
            job = WhatsappJob(
                id=uuid4().hex[:12],
                contact=str(contact or "").strip(),
                message=str(message or ""),
                confirmation_mode=confirmation_mode,
                preview_mode=preview_mode,
                resolved_contact=str(resolved_contact or contact or "").strip(),
                resolved_number=str(resolved_number or "").strip(),
                message_id=str(message_id or uuid4().hex[:12]),
                context_item_id=context_item_id,
                first_person=bool(first_person),
            )
            cls._current = job
            return job

    @classmethod
    def get_pending(cls) -> WhatsappJob | None:
        with cls._lock:
            return cls._current

    @classmethod
    def has_pending(cls) -> bool:
        with cls._lock:
            return cls._current is not None and cls._current.delivery_state in _ACTIVE_STATES

    @classmethod
    def is_awaiting_confirmation(cls) -> bool:
        with cls._lock:
            return cls._current is not None and cls._current.delivery_state == WAITING_CONFIRMATION

    @classmethod
    def get(cls, job_id: str) -> WhatsappJob | None:
        with cls._lock:
            if cls._current is not None and cls._current.id == job_id:
                return cls._current
            return None

    @classmethod
    def transition(
        cls,
        job_id: str,
        *,
        delivery_state: str | None = None,
        speech_state: str | None = None,
        **fields: object,
    ) -> WhatsappJob | None:
        """Atualiza estado/campos do job atual. Retorna o job ou None se ja sumiu."""
        with cls._lock:
            job = cls._current
            if job is None or job.id != job_id:
                return None
            if delivery_state is not None:
                job.delivery_state = delivery_state
            if speech_state is not None:
                job.speech_state = speech_state
            for key, value in fields.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            return job

    @classmethod
    def destroy(cls, job_id: str | None = None) -> None:
        """Descarta o job atual. Se job_id for dado, so descarta se bater."""
        with cls._lock:
            if job_id is None or (cls._current is not None and cls._current.id == job_id):
                cls._current = None
