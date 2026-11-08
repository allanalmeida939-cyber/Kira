// Rótulo de quem falou na transcrição: "VOZ · Chefe", "VOZ · desconhecida", "VOZ · Maria".
// O agente identifica a voz quando o trecho termina; a fala transcrita pode chegar antes ou depois: o rótulo vai para
// a fala por voz do usuário que apareceu nos últimos 10 s, ou espera até 8 s pela próxima.

const RECENT_MS = 10000;
const PENDING_MS = 8000;

export function createVoiceLabel({ documentRef = globalThis.document, windowRef = globalThis.window, now = () => Date.now() } = {}) {
  const doc = documentRef;
  let pending = null;
  let lastUser = null;
  const seen = new WeakSet(); // a transcrição reordena (reinsere) mensagens: só a primeira aparição conta

  const apply = (label) => {
    const message = lastUser?.node;
    const badge = message?.querySelector('.left-transcription-source-badge');
    if (!message || message.dataset.voiceLabel || now() - lastUser.at > RECENT_MS) return false;
    if (message.dataset.device === 'celular') return true; // fica "VOZ · CELULAR"
    if (!badge || !String(badge.textContent).startsWith('VOZ')) return false;
    message.dataset.voiceLabel = label;
    badge.textContent = `VOZ · ${label}`;
    badge.classList.toggle('is-unknown-voice', label === 'desconhecida');
    return true;
  };

  const api = {
    set(label) {
      const clean = String(label || '').trim().slice(0, 30);
      if (!clean) return false;
      if (apply(clean)) return true;
      pending = { label: clean, at: now() };
      return false;
    },
    noteMessage(node) {
      const classes = String(node?.className || '').split(' ');
      if (!classes.includes('user') || classes.includes('partial') || seen.has(node)) return; // parcial é trocada pela final
      seen.add(node);
      lastUser = { node, at: now() };
      if (pending && now() - pending.at <= PENDING_MS && apply(pending.label)) pending = null;
    },
  };
  const container = doc.getElementById('left-transcription-messages');
  if (container && windowRef.MutationObserver) {
    new windowRef.MutationObserver((records) => records.forEach((r) => r.addedNodes.forEach((n) => api.noteMessage(n))))
      .observe(container, { childList: true });
  }
  return api;
}

if (globalThis.document?.getElementById?.('left-transcription-messages') && globalThis.window && !globalThis.window.KiraVoiceLabel) {
  globalThis.window.KiraVoiceLabel = createVoiceLabel();
}
