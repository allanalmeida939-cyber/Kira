// Partes puras do bridge (sem rede): achar o contato, montar o JID e o texto de uma mensagem recebida.

export function fold(value) {
  return String(value ?? '').normalize('NFKD').replace(/[̀-ͯ]/g, '').trim().toLowerCase();
}

/** "(85) 99999-1234" -> "5585999991234". Número brasileiro sem DDI ganha 55; null se não parece telefone. */
export function normalizePhone(value) {
  const digits = String(value ?? '').replace(/\D/g, '');
  if (digits.length < 8) return null;
  if (digits.length === 10 || digits.length === 11) return `55${digits}`;
  return digits;
}

/** Número da própria conta a partir do id do Baileys ("5585912345678:12@s.whatsapp.net"). */
export function phoneFromJid(jid) {
  return normalizePhone(String(jid ?? '').split(':')[0].split('@')[0]);
}

export const phoneJid = (digits) => `${digits}@s.whatsapp.net`;

/** Chat do Chefe com o próprio número? No Baileys 7 ele pode vir como 'numero@s.whatsapp.net' ou '...@lid'. */
export function isOwnChat(key, user) {
  if (!key || !user?.id) return false;
  const phone = phoneFromJid(user.id);
  const lid = user.lid ? phoneFromJid(user.lid) : '';
  return [key.remoteJid, key.remoteJidAlt].some((jid) => {
    const value = String(jid || '');
    if (value.endsWith('@s.whatsapp.net')) return Boolean(phone) && phoneFromJid(value) === phone;
    if (value.endsWith('@lid')) return Boolean(lid) && phoneFromJid(value) === lid;
    return false;
  });
}

/** Mesmo celular? Compara os dígitos e aceita o 9 a mais ou a menos dos celulares brasileiros (contas antigas). */
export function samePhone(a, b) {
  const x = normalizePhone(a);
  const y = normalizePhone(b);
  if (!x || !y) return false;
  const without9 = (digits) => (digits.length === 13 && digits.startsWith('55') && digits[4] === '9' ? digits.slice(0, 4) + digits.slice(5) : digits);
  return x === y || without9(x) === without9(y);
}

/**
 * Quem da lista da Kira mandou a mensagem (o nome na lista) ou null. Contato com número na lista: pelo número.
 * Sem número: pelo nome salvo na agenda do celular do Chefe, se só um contato da agenda bate. O nome que a própria
 * pessoa põe no WhatsApp ("notify") não vale: qualquer um pode se chamar "Amye".
 */
export function kiraContactFor(jid, { kiraContacts = [], synced = new Map() } = {}) {
  const id = String(jid ?? '');
  const phone = id.endsWith('@s.whatsapp.net') ? phoneFromJid(id) : null;
  let saved = null;
  for (const contact of kiraContacts) {
    const name = String(contact?.name ?? '').trim();
    if (!name) continue;
    const listed = contact.phone || contact.number;
    if (listed) {
      if (phone && samePhone(listed, phone)) return name;
      continue;
    }
    saved ||= [...synced.entries()].map(([key, info]) => [key, fold(info?.name)]).filter(([, value]) => value);
    const names = new Set([contact.name, contact.whatsapp_name, ...(contact.aliases || [])].map(fold).filter(Boolean));
    let found = saved.filter(([, value]) => names.has(value));
    if (!found.length) found = saved.filter(([, value]) => names.has(value.split(/\s+/)[0])); // "Amye ❤️" na agenda
    if (found.length === 1 && found[0][0] === id) return name;
  }
  return null;
}
const RECENT_GAP_SECONDS = 7 * 24 * 3600;

/**
 * Procura quem é "contact": telefone direto, a lista da Kira (configs/whatsapp_contacts.json, com apelidos) ou os
 * contatos sincronizados do WhatsApp. Devolve {jid?, phone?, name} ou {error, candidates}.
 */
export function resolveContact(query, { kiraContacts = [], synced = new Map() } = {}) {
  const raw = String(query ?? '').trim();
  if (!raw) return { error: 'contato vazio', candidates: [] };
  if (/^[\d\s()+-]+$/.test(raw)) {
    const phone = normalizePhone(raw);
    return phone ? { phone, jid: phoneJid(phone), name: raw } : { error: 'número inválido', candidates: [] };
  }
  const wanted = fold(raw);
  for (const contact of kiraContacts) {
    const names = [contact.name, contact.whatsapp_name, ...(contact.aliases || [])].map(fold);
    if (names.includes(wanted)) {
      const phone = normalizePhone(contact.phone);
      if (phone) return { phone, jid: phoneJid(phone), name: contact.name };
      // sem número: pesquisa na agenda do celular pelo nome salvo lá ("Vanessa" está como "Sunshine")
      if (contact.whatsapp_name && fold(contact.whatsapp_name) !== wanted) return resolveContact(contact.whatsapp_name, { synced });
    }
  }
  // "Breno final 1234" / "Breno 1234": o final do número separa contatos com o mesmo nome
  const suffix = wanted.match(/^(.*?)\s+(?:final\s+)?(\d{3,})$/);
  const name = suffix ? suffix[1].trim() : wanted;
  const entries = [...synced.entries()].map(([jid, info]) => ({ jid, name: info.name || info.notify || '', folded: fold(info.name || info.notify || ''), last: Number(info.last || 0) }))
    .filter((entry) => entry.folded && (!suffix || entry.jid.split('@')[0].endsWith(suffix[2])));
  const exact = entries.filter((entry) => entry.folded === name);
  const starts = exact.length ? exact : entries.filter((entry) => entry.folded.split(/\s+/)[0] === name || entry.folded.startsWith(`${name} `));
  const found = starts.length ? starts : entries.filter((entry) => entry.folded.includes(name));
  if (found.length === 1) return { jid: found[0].jid, name: found[0].name };
  if (found.length > 1) {
    // Mesmo nome na agenda: fica com quem tem conversa, se só um tem (ou se é bem mais recente que os outros).
    const byRecent = [...found].sort((a, b) => b.last - a.last);
    if (byRecent[0].last && byRecent[0].last - (byRecent[1].last || 0) > RECENT_GAP_SECONDS) return { jid: byRecent[0].jid, name: byRecent[0].name };
    const labels = byRecent.slice(0, 5).map((entry) => `${entry.name} final ${entry.jid.split('@')[0].slice(-4)}`);
    return { error: `tem mais de um contato com esse nome (${labels.join(', ')}); diga o final do número`, candidates: labels };
  }
  return { error: 'contato não encontrado', candidates: [] };
}

/** Texto de uma mensagem do Baileys; mídia vira uma etiqueta ("[foto] legenda"). null para o que não interessa. */
export function messageText(message) {
  // visualização única é só para o Chefe ver: a Kira nem abre (2026-11-07)
  if (message?.viewOnceMessage || message?.viewOnceMessageV2 || message?.viewOnceMessageV2Extension) return '[visualização única: só o Chefe pode ver]';
  const m = message?.ephemeralMessage?.message || message;
  if (!m) return null;
  if (m.call || m.bcallMessage || m.callLogMesssage || m.callLogMessage) return '[ligação]';
  if (m.conversation) return m.conversation;
  if (m.extendedTextMessage?.text) return m.extendedTextMessage.text;
  const tagged = [
    ['imageMessage', '[foto]'], ['videoMessage', '[vídeo]'], ['audioMessage', '[áudio]'],
    ['documentMessage', '[arquivo]'], ['stickerMessage', '[figurinha]'], ['contactMessage', '[contato]'],
    ['locationMessage', '[localização]'],
  ];
  for (const [key, tag] of tagged) {
    if (m[key]) {
      const extra = m[key].caption || m[key].fileName || '';
      return extra ? `${tag} ${extra}` : tag;
    }
  }
  return null;
}

const MEDIA_KINDS = {
  image: ['jpg', 'jpeg', 'png', 'webp', 'gif'],
  video: ['mp4', 'mov', 'mkv', 'webm', '3gp'],
  audio: ['mp3', 'ogg', 'm4a', 'wav', 'opus', 'aac'],
};

/** Tipo de envio pelo arquivo; o resto vai como documento. */
export function mediaKind(path) {
  const ext = String(path).split('.').pop().toLowerCase();
  return Object.entries(MEDIA_KINDS).find(([, list]) => list.includes(ext))?.[0] || 'document';
}

/** Fila com limite: mensagens novas para a interface e para o agente (cada um consome a sua). */
export class Inbox {
  constructor(limit = 200) {
    this.limit = limit;
    this.items = [];
  }

  push(item) {
    this.items.push(item);
    if (this.items.length > this.limit) this.items.splice(0, this.items.length - this.limit);
  }

  drain() {
    return this.items.splice(0, this.items.length);
  }
}
