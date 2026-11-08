"""orchestrator.py — o cerebro do WhatsApp V9.

Une job_store + confirmation + sender + speech_policy. Substitui a logica que
vivia inchada no assistant.py (propose_whatsapp_reply / handle_outbound_whatsapp_intent
/ process_whatsapp_confirmation). O assistant vira so roteador: ao chegar intent
WhatsApp pendente, chama `orchestrator.handle(transcript)`; a tool enviar_whatsapp
chama `orchestrator.create_job(...)`.

ROTEAMENTO DE FALA (importante p/ nao falar em dobro):
  • create_job (chamada de tool): RETORNA string -> o Gemini fala (previa, ou ack
    do envio-direto). Caminho comprovado do V8.
  • handle (gate confirmou "pode mandar"): NAO ha retorno pro Gemini -> a fala sai
    DIRETO via speech_policy (pre_ack + sucesso/falha), deterministica.
Esse split precisa de teste de voz ao vivo p/ ajuste fino do double-speak.

Reusa helpers que PERMANECEM no assistant (passado como `speaker`):
  _resolve_whatsapp_contact_for_send, append_whatsapp_context_note,
  _last_whatsapp_contact/_message/_last_confirmed_whatsapp_context,
  _whatsapp_send_cooldown, _speech_coordinator (via speech_policy).
"""

from __future__ import annotations

import logging
import os
import re
import time

from ..whatsapp_context import fold_whatsapp_text, normalize_whatsapp_text
from . import confirmation, sender, speech_policy
from .job_store import (
    CONFIRM_REQUIRED,
    CONFIRM_SKIP,
    FAILED,
    POST_ACK,
    PRE_ACK,
    PREVIEW_SHOW,
    SENDING,
    SENT,
    WAITING_CONFIRMATION,
    WAITING_CONTACT,
    WhatsappJobStore,
)

logger = logging.getLogger("core.whatsapp.orchestrator")

# A fala que pediu o envio chega ao gate junto com a tool: não é resposta à prévia (sessão de 2026-10-24 11:07, o
# próprio pedido virou "cancelar" e o "pode" seguinte criou outro envio, com prévia repetida).
SAME_TURN_SECONDS = 3.0
OWNER_NAME = (os.getenv("KIRA_OWNER_NAME") or "Chefe").strip() or "Chefe"

_KIRA_PREFIX_RE = re.compile(r"^\s*kira:\s*", re.IGNORECASE)


def _strip_kira(text: str) -> str:
    raw = str(text or "")
    return _KIRA_PREFIX_RE.sub("", raw).strip() or raw


def _apply_perspective(message: str, first_person_hint: bool = False) -> tuple[str, bool]:
    """Decide a assinatura conforme a perspectiva.

    PADRAO (recado/aviso E "como se fosse VOCE" = a propria Kira falando) -> assina
    "Kira: ". E a Kira quem manda, entao ela se identifica; ela mantem o prefixo mesmo
    quando fala "do jeito dela". SOMENTE quando o usuario pede "como se fosse EU / o
    Chefe / em meu nome" (first_person_hint=True, vindo da tool) a mensagem sai SEM
    "Kira:" — parece escrita pelo proprio usuario.

    NAO ha mais deteccao por conteudo da mensagem: so o hint explicito da tool decide.
    Retorna (mensagem_assinada, impersona_usuario).
    """
    msg = str(message or "").strip()
    starts_kira = msg.lower().startswith("kira:")
    body = msg[len("kira:"):].strip() if starts_kira else msg
    if first_person_hint:
        # impersonando o usuario (Chefe): mensagem SEM assinatura "Kira:"
        return body, True
    # qualquer outro caso (inclusive "como se fosse voce" = Kira): MANTEM "Kira:"
    return (msg if starts_kira else f"Kira: {msg}"), False


class WhatsappOrchestrator:
    def __init__(self, speaker, session=None) -> None:
        self._speaker = speaker
        self._session = session

    # ---- API publica -------------------------------------------------------
    async def create_job(
        self,
        *,
        contact: str,
        message: str,
        confirmation_mode: str = CONFIRM_REQUIRED,
        preview_mode: str = PREVIEW_SHOW,
        first_person: bool = False,
    ) -> str:
        """Chamado pela tool enviar_whatsapp. Retorna a string que o Gemini fala."""
        speaker = self._speaker
        normalized_contact = normalize_whatsapp_text(contact) if contact else ""
        if not normalized_contact:
            normalized_contact = str(getattr(speaker, "_last_whatsapp_contact", "") or "").strip()
        if not normalized_contact:
            return "Nao sei para qual contato enviar a mensagem no WhatsApp."

        signed_message, first_person = _apply_perspective(message, first_person_hint=first_person)
        normalized_message = normalize_whatsapp_text(signed_message)
        normalized_message, placeholder = fill_clipboard_placeholders(normalized_message)
        normalized_message = attach_clipboard_if_asked(
            normalized_message, str(getattr(self._speaker, "_last_user_utterance", "") or ""))
        if placeholder:  # sobrou "[Link ...]": nunca manda marcador no lugar do conteúdo de verdade
            return (f"Não mandei: a mensagem ficou com '{placeholder}' no lugar do conteúdo. Me diga o texto ou o link "
                    "certo (ou copie de novo e peça para mandar o que está no seu copiar).")
        if not normalized_message or len(_strip_kira(normalized_message)) < 2:
            return f"Nao consegui montar uma mensagem valida para {normalized_contact}."

        # resolucao de contato (helper permanece no assistant)
        found, resolved_name, resolved_number = self._resolve_contact(normalized_contact)
        if not found:
            # Contato nao encontrado: NAO descarta — cria job WAITING_CONTACT guardando a
            # mensagem e os modos. No proximo turno o usuario diz o nome certo e handle()
            # resolve (fluxo "confirma o nome"). has_pending fica True -> o turno seguinte
            # cai no gate WhatsApp.
            job = WhatsappJobStore.create(
                contact=normalized_contact,
                message=normalized_message,
                confirmation_mode=confirmation_mode,
                preview_mode=preview_mode,
                resolved_contact="",
                resolved_number="",
                first_person=first_person,
            )
            WhatsappJobStore.transition(job.id, delivery_state=WAITING_CONTACT)
            logger.info("WHATSAPP_JOB_WAITING_CONTACT job=%s query=%s", job.id, normalized_contact)
            return f"Não encontrei {normalized_contact} nos contatos do WhatsApp, chefe. Qual é o nome certo?"
        normalized_contact = resolved_name
        problem = self._message_problem(normalized_contact, normalized_message, first_person)
        if problem:
            return problem

        # FASE 3 — trava anti-reenvio fantasma. So honra "manda direto" (skip, sem
        # confirmacao) se o turno ATUAL do usuario for mesmo um pedido de WhatsApp. Se o
        # Gemini chamar enviar_whatsapp por conta propria num turno comum (ex.: "kira como
        # ta as coisas?"), NAO manda silencioso: rebaixa pra exigir confirmacao -> vira um
        # "posso mandar?" visivel e bloqueavel, em vez de disparar sozinho.
        if confirmation_mode == CONFIRM_SKIP and not self._turn_requests_whatsapp(normalized_contact):
            logger.warning(
                "WHATSAPP_SKIP_GUARD_DOWNGRADE contact=%s reason=turn_not_whatsapp_request",
                normalized_contact,
            )
            confirmation_mode = CONFIRM_REQUIRED
            preview_mode = PREVIEW_SHOW

        job = WhatsappJobStore.create(
            contact=normalized_contact,
            message=normalized_message,
            confirmation_mode=confirmation_mode,
            preview_mode=preview_mode,
            resolved_contact=normalized_contact,
            resolved_number=resolved_number,
            first_person=first_person,
        )
        logger.info(
            "WHATSAPP_JOB_CREATED job=%s contact=%s confirm=%s preview=%s",
            job.id, normalized_contact, confirmation_mode, preview_mode,
        )
        spoken = _strip_kira(normalized_message)

        # ENVIO DIRETO (skip_confirmation): manda na hora; Gemini fala o ack (retorno).
        if confirmation_mode == CONFIRM_SKIP:
            if not self._dedupe_allows(normalized_contact, normalized_message):
                WhatsappJobStore.destroy(job.id)
                logger.warning("WHATSAPP_DIRECT_SEND_BLOCKED reason=dedupe contact=%s", normalized_contact)
                return "Essa mesma mensagem ja foi enviada agora ha pouco, chefe. Nao vou mandar de novo pra nao duplicar."
            _result, _ack_text = await self._execute_send(job, speak=False)
            # Sucesso fala UMA vez so, pela confirmacao NATURAL do Gemini (nota
            # de envio ja injetada no contexto). Sem speak_ack deterministico
            # (que dobrava) e sem pre-ack. Em falha, devolve o texto de erro pro Gemini.
            if _result.ok:
                return ""  # silencio aqui; o Gemini confirma via nota
            return _ack_text

        # CONFIRMACAO requerida: cria pendencia e espera o gate (handle).
        WhatsappJobStore.transition(job.id, delivery_state=WAITING_CONFIRMATION)
        logger.info("WHATSAPP_CONFIRMATION_CREATED token=%s", job.id)
        await self._append_note(
            "(registro interno, nao leia em voz alta) Existe um envio de WhatsApp pendente de confirmacao explicita. "
            "Nunca envie WhatsApp sem o usuario dizer claramente para mandar. Anuncie de "
            "forma curta e natural que vai mandar, leia a mensagem entre aspas e pergunte se "
            "pode mandar. NAO fale o prefixo tecnico 'Kira:' (so existe no texto real do "
            f"WhatsApp). A mensagem a anunciar e: \"{spoken}\"."
        )
        return "AINDA NAO ENVIADO (nao diga que mandou): " + speech_policy.build_preview_text(job, spoken)

    async def handle(self, transcript: str) -> dict:
        """Chamado pelo assistant quando ha intent WhatsApp e job aguardando.

        confirm -> envia (fala direto). cancel/unrelated -> derruba e segue conversa.
        Retorna dict com handled/sent/decision.
        """
        job = WhatsappJobStore.get_pending()
        if job is None:
            return {"handled": False, "reason": "no_pending"}
        if time.time() - job.created_at < SAME_TURN_SECONDS:
            logger.info("WHATSAPP_CONFIRMATION_SAME_TURN_IGNORED job=%s", job.id)
            return {"handled": False, "reason": "same_turn"}

        # Fluxo "confirma o nome": contato nao foi achado e o usuario esta dizendo o certo.
        if job.delivery_state == WAITING_CONTACT:
            return await self._handle_contact_correction(job, transcript)

        if job.delivery_state != WAITING_CONFIRMATION:
            return {"handled": False, "reason": "no_pending"}

        # O próprio pedido ("manda mensagem pro Breno ...") volta pelo roteador depois de esperar a fala dela terminar
        # e o "manda" contava como "pode mandar": a mensagem saía sem o Chefe confirmar. Pedido novo não é confirmação.
        folded = " ".join(str(transcript or "").lower().split())
        if re.search(r"\bmensage\w*\s+(pr[oa]|para|pr[oa]s?\s+o|pro\s+a|no|na)\b", folded):
            logger.info("WHATSAPP_CONFIRMATION_NEW_COMMAND_IGNORED job=%s", job.id)
            return {"handled": False, "reason": "new_command"}

        decision = confirmation.classify(transcript)
        logger.info("WHATSAPP_CONFIRMATION_DECISION job=%s decision=%s", job.id, decision)
        if decision == "confirm":
            if not self._dedupe_allows(job.contact, job.message):
                WhatsappJobStore.destroy(job.id)
                return {
                    "handled": True, "sent": False, "reason": "dedupe",
                    "speak_text": "Essa mesma mensagem ja foi enviada ha pouco, chefe. Nao vou duplicar.",
                }
            result, ack_text = await self._execute_send(job, speak=True)
            return {"handled": True, "sent": bool(result.ok), "speak_text": ack_text}

        # qualquer coisa que NAO seja confirmacao -> cancela e segue a conversa normal
        WhatsappJobStore.destroy(job.id)
        logger.info("WHATSAPP_CONFIRMATION_DROPPED job=%s decision=%s", job.id, decision)
        return {"handled": False, "cancelled": True, "decision": decision}

    def _message_problem(self, contact: str, message: str, first_person: bool) -> str:
        """Motivo para não criar o envio ('' = segue): repetição do envio pendente ou mensagem falando com o Chefe."""
        body = fold_whatsapp_text(_strip_kira(message))
        pending = WhatsappJobStore.get_pending()
        if (pending is not None and pending.delivery_state == WAITING_CONFIRMATION
                and fold_whatsapp_text(pending.resolved_contact) == fold_whatsapp_text(contact)
                and fold_whatsapp_text(_strip_kira(pending.message)) == body):
            logger.info("WHATSAPP_JOB_DUPLICATE_IGNORED job=%s", pending.id)
            return (f"Essa mensagem para {contact} já está esperando a confirmação do {OWNER_NAME}; o sistema envia "
                    "sozinho quando ele confirmar. Não leia a prévia de novo nem pergunte outra vez.")
        owner = fold_whatsapp_text(OWNER_NAME)
        # vocativo ("Chefe, oi!", "Oi Chefe"); "Chefe te chamou" é 3ª pessoa e passa
        addressed = re.match(rf"^(?:(?:oi|ola|opa|e ai|fala|ei|bom dia|boa tarde|boa noite)\W+{re.escape(owner)}\b|{re.escape(owner)}\s*[,!:])", body)
        if addressed and not first_person and not fold_whatsapp_text(contact).startswith(owner):
            logger.info("WHATSAPP_MESSAGE_ADDRESSES_OWNER contact=%s", contact)
            return (f"Não enviei: a mensagem fala com o {OWNER_NAME}, mas vai para {contact}. Reescreva como a Kira "
                    f"falando com {contact} (o {OWNER_NAME} sempre na 3ª pessoa) e chame enviar_whatsapp de novo.")
        return ""

    @staticmethod
    def has_pending() -> bool:
        return WhatsappJobStore.has_pending()

    # ---- internos ----------------------------------------------------------
    def _turn_requests_whatsapp(self, contact: str = "") -> bool:
        """True se a fala ATUAL do usuario parece um pedido de WhatsApp. Usado pela trava
        anti-reenvio fantasma (FASE 3). LENIENTE de proposito: so retorna False quando NAO
        ha sinal nenhum de envio (ex.: 'kira como ta as coisas?') — assim nao rebaixa um
        'manda direto pra fulano' legitimo. Sinais aceitos: verbo de envio, termo de
        whatsapp, ou o nome do contato citado na fala."""
        utt = str(getattr(self._speaker, "_last_user_utterance", "") or "").strip().lower()
        if not utt:
            return False
        send_terms = (
            "manda", "mande", "mandar", "envia", "envie", "enviar", "mensagem", "msg",
            "avisa", "avise", "avisar", "responde", "responder", "fala pra", "fala pro",
            "diz pra", "diz pro", "whatsapp", "wpp", "zap",
        )
        if any(term in utt for term in send_terms):
            return True
        # resposta curta ao "posso mandar?" que a própria Kira fez ("pode", "sim", "isso") também é pedido de envio
        # (2026-11-07: o "pode" virava outra confirmação e a Kira dizia "mandei" sem ter mandado)
        words = utt.strip(" .!?,").split()
        if words and len(words) <= 4 and words[0] in {"pode", "sim", "isso", "manda", "envia", "bora", "ok", "beleza", "claro", "confirmo", "positivo"}:
            return True
        first_name = str(contact or "").strip().lower().split()[:1]
        if first_name and first_name[0] and first_name[0] in utt:
            return True
        return False

    def _resolve_contact(self, query: str) -> tuple[bool, str, str]:
        """Resolve um nome de contato.

        Retorna SEMPRE (True, nome, numero). O arquivo local de contatos
        (_resolve_whatsapp_contact_for_send) e so um CACHE de apelidos/numeros: se
        achar, usa o nome/numero canonico; se NAO achar (ou der erro/ausente), NAO
        bloqueia — usa o proprio nome dito pelo usuario e deixa a BRIDGE do WhatsApp
        Web pesquisar na busca (ela acha qualquer contato salvo pelo nome). Se o
        contato nao existir mesmo, o /send falha e a Kira avisa que nao conseguiu.
        """
        resolve = getattr(self._speaker, "_resolve_whatsapp_contact_for_send", None)
        if callable(resolve):
            logger.info("WHATSAPP_CONTACT_SEARCH_BEGIN")
            try:
                resolved = resolve(query)
            except Exception as exc:
                logger.warning("WHATSAPP_CONTACT_RESOLVE_FAILED error=%s", exc)
                resolved = None
            if isinstance(resolved, dict) and resolved.get("found"):
                cdata = resolved.get("contact") if isinstance(resolved.get("contact"), dict) else {}
                # pesquisa pelo nome salvo no celular ("Vanessa" está como "Sunshine"), não pelo jeito que o Chefe chama
                name = str(cdata.get("whatsapp_name") or cdata.get("name") or query).strip() or query
                number = str(cdata.get("number") or cdata.get("phone") or "").strip()
                logger.info("WHATSAPP_CONTACT_FOUND contact=%s", name)
                return True, name, number
            logger.info("WHATSAPP_CONTACT_LOCAL_MISS_USING_BRIDGE contact=%s", query)
        return True, query, ""

    async def _handle_contact_correction(self, job, transcript: str) -> dict:
        """Job em WAITING_CONTACT: o usuario esta dizendo o nome certo do contato."""
        if confirmation.classify(transcript) == "cancel":
            WhatsappJobStore.destroy(job.id)
            logger.info("WHATSAPP_WAITING_CONTACT_CANCELLED job=%s", job.id)
            return {"handled": False, "cancelled": True, "decision": "cancel"}

        candidate = normalize_whatsapp_text(transcript) or transcript.strip()
        found, name, number = self._resolve_contact(candidate)
        if not found:
            WhatsappJobStore.destroy(job.id)
            logger.info("WHATSAPP_WAITING_CONTACT_NOT_FOUND job=%s query=%s", job.id, candidate)
            return {
                "handled": True, "sent": False,
                "speak_text": f"Também não achei {candidate} nos contatos, chefe. Tenta de novo dizendo o comando inteiro.",
            }

        WhatsappJobStore.transition(job.id, contact=name, resolved_contact=name, resolved_number=number)
        logger.info("WHATSAPP_WAITING_CONTACT_RESOLVED job=%s contact=%s", job.id, name)
        spoken = _strip_kira(job.message)
        if job.confirmation_mode == CONFIRM_SKIP:
            if not self._dedupe_allows(name, job.message):
                WhatsappJobStore.destroy(job.id)
                return {"handled": True, "sent": False,
                        "speak_text": "Essa mesma mensagem ja foi enviada ha pouco, chefe. Nao vou duplicar."}
            result, ack_text = await self._execute_send(job, speak=True)
            return {"handled": True, "sent": bool(result.ok), "speak_text": ack_text}
        WhatsappJobStore.transition(job.id, delivery_state=WAITING_CONFIRMATION)
        return {"handled": True, "sent": False, "speak_text": speech_policy.build_preview_text(job, spoken)}

    async def _execute_send(self, job, *, speak: bool):
        """Envia e retorna (SendResult, ack_text).

        SUCESSO: nao fala NADA aqui. NAO ha pre-ack ("mandando...") — o usuario nao quer.
        A UNICA confirmacao de sucesso e a fala NATURAL do Gemini, disparada pela nota
        de envio abaixo (o gate defere o turno pro Gemini). ack_text e usado
        so no caminho de FALHA (falado deterministicamente pelo chamador).
        `speak` mantido por compatibilidade dos chamadores; nao dispara mais fala."""
        WhatsappJobStore.transition(job.id, delivery_state=SENDING, speech_state=PRE_ACK)
        result = await sender.send(job)
        self._record_dedupe(job, result.ok)
        if result.ok:
            WhatsappJobStore.transition(job.id, delivery_state=SENT, speech_state=POST_ACK)
            self._remember_success(job)
            _spoken = _strip_kira(job.message)
            # Nota: a Kira (Gemini) da a UNICA confirmacao, em UMA frase curta e natural.
            await self._append_note(
                f"(registro interno, nao leia em voz alta) A mensagem de WhatsApp FOI ENVIADA de verdade para {job.resolved_contact}. "
                f"Conteudo enviado: \"{_spoken}\". Responda com UMA UNICA frase curta e natural "
                f"(do seu jeito) confirmando que enviou e comentando algo sobre a mensagem. "
                f"NAO diga que esta 'mandando' (ja foi enviado), NAO confirme duas vezes, NAO "
                f"repita, NAO chame enviar_whatsapp de novo."
            )
            logger.info("WHATSAPP_MESSAGE_SENT_OK job=%s", job.id)
            ack_text = speech_policy.build_success_text(job)
        else:
            reason = (result.user_message or result.error or "").strip()
            WhatsappJobStore.transition(job.id, delivery_state=FAILED, error=reason)
            await self._append_note(
                f"(registro interno, nao leia em voz alta) A tentativa de enviar a mensagem de WhatsApp para {job.resolved_contact} FALHOU "
                f"(motivo tecnico: {result.error or 'desconhecido'}). A mensagem NAO foi entregue. "
                f"Avise o usuario com naturalidade que nao conseguiu enviar agora."
            )
            logger.info("WHATSAPP_MESSAGE_SEND_FAILED_NON_FATAL job=%s reason=%s", job.id, result.error or "?")
            ack_text = speech_policy.build_failure_text(job, reason)
        WhatsappJobStore.destroy(job.id)
        return result, ack_text

    def _remember_success(self, job) -> None:
        sp = self._speaker
        try:
            sp._last_whatsapp_contact = job.resolved_contact
            sp._last_whatsapp_message = _strip_kira(job.message)
            sp._last_confirmed_whatsapp_context = {
                "contact": job.resolved_contact,
                "message": _strip_kira(job.message),
                "message_id": job.message_id,
                "success": True,
            }
            cooldown = getattr(sp, "_whatsapp_send_cooldown", None)
            if isinstance(cooldown, dict):
                cooldown[fold_whatsapp_text(job.resolved_contact)] = time.time()
        except Exception as exc:  # pragma: no cover
            logger.debug("WHATSAPP_REMEMBER_SUCCESS_SKIPPED error=%s", exc)

    async def _append_note(self, text: str) -> None:
        fn = getattr(self._speaker, "append_whatsapp_context_note", None)
        if callable(fn):
            try:
                await fn(text)
            except Exception as exc:  # pragma: no cover
                logger.debug("WHATSAPP_CONTEXT_NOTE_SKIPPED error=%s", exc)

    @staticmethod
    def _dedupe_allows(contact: str, message: str) -> bool:
        try:
            from services.whatsapp_runtime_service import check_send_dedupe
            return bool(check_send_dedupe(contact, message, "").get("allowed", True))
        except Exception:
            return True

    @staticmethod
    def _record_dedupe(job, success: bool) -> None:
        try:
            from services.whatsapp_runtime_service import record_send_result
            record_send_result(job.contact, job.message, "", bool(success))
        except Exception:
            pass


_PLACEHOLDER_RE = __import__("re").compile(r"\[[^\[\]]{2,60}\]")
_CLIPBOARD_WORDS = ("clipboard", "copiad", "copiar", "area de transfer", "área de transfer", "link", "url", "colad")


def _read_clipboard_text() -> str:
    try:
        import win32clipboard  # type: ignore
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or "").strip()
        finally:
            win32clipboard.CloseClipboard()
    except Exception:  # noqa: BLE001 - sem acesso ao copiar/colar: fica o marcador (e o envio é recusado)
        return ""
    return ""


def fill_clipboard_placeholders(message: str, reader=_read_clipboard_text) -> tuple[str, str]:
    """'[Link do Clipboard]' vira o que o Chefe copiou de verdade. Devolve (mensagem, marcador que sobrou ou '')."""
    text = str(message or "")
    clip = None
    for hit in list(_PLACEHOLDER_RE.finditer(text)):
        token = hit.group(0)
        if any(w in token.lower() for w in _CLIPBOARD_WORDS):
            if clip is None:
                clip = reader()[:2000]
            if clip:
                text = text.replace(token, clip)
    rest = next((h.group(0) for h in _PLACEHOLDER_RE.finditer(text)
                 if any(w in h.group(0).lower() for w in _CLIPBOARD_WORDS + ("nome", "contato", "numero", "número", "aqui"))), "")
    return text, rest


_ASKS_CLIPBOARD = __import__("re").compile(
    r"(?i)(copi|clipboard|rea de transfer|ctrl ?\+? ?c)")


def attach_clipboard_if_asked(message: str, request_text: str, reader=_read_clipboard_text) -> str:
    """Pedido fala em "o que copiei / meu copiar" e a mensagem não traz o conteúdo: cola o conteúdo no fim
    (2026-11-07: a Kira escrevia "o link que ele copiou" e mandava sem o link)."""
    text = str(message or "")
    if not (_ASKS_CLIPBOARD.search(request_text or "") or _ASKS_CLIPBOARD.search(text)):
        return text
    clip = reader()[:2000]
    if not clip or clip in text:
        return text
    return text.rstrip() + "\n" + clip

