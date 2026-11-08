from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field

SENSITIVE_TERMS = {
    "morrer","morte","hospital","doenca","remedio","senha","pix","dinheiro","conta","banco",
    "ameaca","briga","termino","traicao","demissao","urgente","socorro"
}

@dataclass
class ToolAckTemplateBank:
    _recent_ids: deque[str] = field(default_factory=lambda: deque(maxlen=10))
    _last_comment_at: float = 0.0

    templates = {
        "whatsapp_send_success": ["Prontinho, mandei para {contact}: {message}.","Fechado, mandei para {contact}: {message}.","Ja mandei para {contact}: {message}.","Mensagem enviada para {contact}: {message}.","Pode deixar, mandei para {contact}: {message}.","Tudo certo, mandei para {contact}: {message}.","Enviei agora para {contact}: {message}.","Missao cumprida, mandei para {contact}: {message}.","Ta feito, mandei para {contact}: {message}.","Recado entregue para {contact}: {message}.","Acabei de mandar para {contact}: {message}.","Confirmado, enviei para {contact}: {message}.","Mensagem disparada para {contact}: {message}.","Ja foi para {contact}: {message}.","Ta na mao, mandei para {contact}: {message}.","Pronto, enviei para {contact}: {message}.","Beleza, mandei para {contact}: {message}.","Certo, mandei para {contact}: {message}.","Foi sim, mandei para {contact}: {message}.","Recado enviado com sucesso para {contact}: {message}."],
        "whatsapp_last_send_success": ["Mandei sim para {contact}: {message}.","Foi sim, mandei para {contact}: {message}.","Sim, ja foi para {contact}: {message}.","Sim, a mensagem para {contact} foi enviada.","Confirmado, mandei para {contact}.","Sim, o ultimo envio foi para {contact}: {message}.","Sim, deixei enviado para {contact}.","Foi tudo certo, mandei para {contact}.","Enviei sim, foi para {contact}.","Ja esta enviado para {contact}.","Pode confiar, mandei para {contact}: {message}.","Fechado, o envio para {contact} foi feito.","Sim, despacho concluido para {contact}.","Mandei, e foi para {contact}.","Sim, confirmei envio para {contact}: {message}."],
        "whatsapp_pending_message": ["O que voce quer que eu mande para {contact}?","Qual mensagem eu envio para {contact}?","Me diga o texto que eu mando para {contact}.","Certo, para {contact}. Qual e a mensagem?","Beleza, e o que eu escrevo para {contact}?","Fechado. Qual recado vai para {contact}?","Me fala o conteudo da mensagem para {contact}.","So falta o texto. O que eu mando para {contact}?","Perfeito, para {contact}. Qual texto vai?","Me passa a mensagem que envio para {contact}.","Qual recado voce quer enviar para {contact}?","So me falta o texto para {contact}.","Beleza, me dita a mensagem para {contact}.","Quer mandar o que para {contact}?"],
        "whatsapp_pending_contact": ["Para quem eu mando essa mensagem?","Quem deve receber esse recado?","Certo, mas para qual contato?","Me diga o contato que vai receber.","Qual e o nome da pessoa no WhatsApp?"],
        "whatsapp_contact_not_found": ["Nao encontrei {contact} na agenda do WhatsApp.","Nao achei ninguem com o nome {contact}.","{contact} nao ta nos contatos. Confere o nome?","Nao tenho {contact} salvo. Quer me passar outro nome?"],
        "whatsapp_send_failed_search": ["Nao consegui encontrar {contact} no WhatsApp agora.","O campo de busca do WhatsApp nao respondeu. Tenta de novo?","Nao achei {contact} na lista. Pode ter mudado o nome?"],
        "whatsapp_send_failed_search_box": ["Não consegui abrir a busca do WhatsApp agora. O WhatsApp parece conectado, mas a tela de busca não ficou disponível."],
        "whatsapp_send_failed_contact": ["Não encontrei {contact} no WhatsApp. Confere se o nome está certo?"],
        "whatsapp_send_failed_message_box": ["Consegui abrir a conversa, mas não achei o campo para digitar a mensagem."],
        "whatsapp_send_failed_send_button": ["Digitei a mensagem, mas não consegui acionar o botão de enviar."],
        "whatsapp_send_failed_login": ["O WhatsApp Web parece não estar logado. Precisa abrir e confirmar o login."],
        "whatsapp_send_failed_not_ready": ["O WhatsApp está aberto, mas ainda não está pronto para enviar."],
        "whatsapp_send_failed_unknown": ["Não consegui enviar agora. O erro veio do WhatsApp Web."],
        "whatsapp_send_failed_forbidden": ["Nao consegui enviar para {contact}. O WhatsApp bloqueou esse envio.","O WhatsApp rejeitou a mensagem para {contact}. Confere se a sessao ainda esta valida.","Nao deu para mandar para {contact} agora. O bridge bloqueou o envio."],
        "whatsapp_send_failed": ["Nao consegui mandar para {contact}. O WhatsApp nao respondeu.","Tentei enviar para {contact}, mas a ponte do WhatsApp falhou.","Nao deu para enviar agora. O WhatsApp parece desconectado.","Nao consegui concluir o envio para {contact}.","Falhou no envio para {contact}: {error}.","O recado nao foi enviado. Motivo: {error}.","Nao consegui abrir a conversa de {contact}.","O WhatsApp Web nao confirmou o envio.","Nao rolou enviar para {contact} agora.","Envio interrompido para {contact}: {error}.","Nao foi possivel finalizar o envio para {contact}.","Tentei, mas o WhatsApp nao confirmou para {contact}.","Sem sucesso no envio para {contact}.","O envio para {contact} ficou pendente por falha do WhatsApp."],
        "whatsapp_dry_run_success": ["Simulei o envio para {contact}: {message}.","Em modo teste, simulei o envio para {contact}: {message}.","Dry-run concluido: simulei a mensagem para {contact}.","Teste feito, simulei o envio para {contact}."],
        "whatsapp_contextual_light_comment": ["Essa foi direta ao ponto, hein?","Recado entregue sem drama.","Agora e aguardar a resposta.","Missao cumprida.","Agora e com ela.","Sera que era caso urgente, chefe?"],
        "whatsapp_contact_confirm": ["Voce quer mandar para {contact}?","Mensagem para {contact}?","Encontrei {contact}. Confirma?","E para {contact} mesmo?","Mando para {contact}?"],
        "whatsapp_contact_ambiguous": ["Encontrei {candidates}. Para qual deles?","Temos {candidates}. Para quem vai?","Ha mais de um contato: {candidates}. Qual e?"],
        "whatsapp_contact_confirmation_rejected": ["Tudo bem, cancela o envio. Me diz o contato certo quando quiser.","Ok, esquece esse. Me passa o nome correto.","Cancelei. Me fala para quem eu devo mandar."],
    }

    def select(self, category: str) -> tuple[str, str]:
        options = self.templates.get(category) or ["{message}"]
        indexed = [f"{category}:{i}" for i, _ in enumerate(options)]
        candidates = [idx for idx in indexed if idx not in self._recent_ids] or indexed
        chosen_id = random.choice(candidates)
        self._recent_ids.append(chosen_id)
        template = options[int(chosen_id.rsplit(":", 1)[1])]
        return chosen_id, template

    def should_add_contextual_comment(self, message: str, contact: str) -> bool:
        text = str(message or "").strip().lower()
        if not text or len(text) > 80:
            return False
        if any(term in text for term in SENSITIVE_TERMS):
            return False
        if time.time() - self._last_comment_at < 45.0:
            return False
        return random.random() < 0.28

    def build_contextual_comment(self, message: str, contact: str) -> str:
        self._last_comment_at = time.time()
        _, tpl = self.select("whatsapp_contextual_light_comment")
        return tpl.format(message=message, contact=contact)
