import { renderMarkdown } from './markdown.js';
import { sendChatMessage } from '../chat/sendChatMessage.js';

// Página "Chat da Kira" no mesmo padrão das Configurações: moldura com brilho roxo, cabeçalho com ícone, botões em
// pílula e mensagens em cartões. Só redesenha o que mudou, e só com a página à mostra: antes refazia todas as
// mensagens a cada pedaço da fala da Kira, mesmo escondida (2026-10-25).

const SVG_NS = 'http://www.w3.org/2000/svg';
const ICONS = {
  chat: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z',
  mic: 'M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z M19 10v2a7 7 0 0 1-14 0v-2 M12 19v3',
  power: 'M18.36 6.64a9 9 0 1 1-12.73 0 M12 2v10',
  back: 'M19 12H5 M12 19l-7-7 7-7',
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z M21 21l-4.35-4.35',
  send: 'M22 2 11 13 M22 2l-7 20-4-9-9-4 20-7z',
  copy: 'M9 9h11v11H9z M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1',
  down: 'M12 5v14 M19 12l-7 7-7-7',
};

function create(documentRef, tag, className = '', text = '') {
  const node = documentRef.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function icon(documentRef, name) {
  const svg = documentRef.createElementNS ? documentRef.createElementNS(SVG_NS, 'svg') : documentRef.createElement('svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', 'kui-chat-icon');
  String(ICONS[name] || '').split(' M').forEach((part, index) => {
    const path = documentRef.createElementNS ? documentRef.createElementNS(SVG_NS, 'path') : documentRef.createElement('path');
    path.setAttribute('d', index ? `M${part}` : part);
    svg.appendChild(path);
  });
  return svg;
}

function button(documentRef, className, label, iconName, ariaLabel = label) {
  const node = create(documentRef, 'button', className);
  node.type = 'button';
  if (iconName) node.appendChild(icon(documentRef, iconName));
  if (label) node.appendChild(create(documentRef, 'span', '', label));
  node.setAttribute('aria-label', ariaLabel);
  return node;
}

function isAtEnd(element) {
  return element.scrollHeight - element.scrollTop - element.clientHeight < 48;
}

export function sourceLabel(source) {
  if (source === 'voice') return 'VOZ';
  if (source === 'text') return 'TEXTO';
  return 'SISTEMA';
}

export function timeLabel(timestamp) {
  const date = new Date(Number(timestamp) || 0);
  if (!Number(timestamp) || Number.isNaN(date.getTime())) return '';
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

function isInputTarget(target) {
  return Boolean(target?.matches?.('input, textarea, select, [contenteditable="true"]'));
}

export class ChatView {
  constructor({ store, bus = null, documentRef = globalThis.document, windowRef = globalThis.window, fetchRef = null, onNavigate = null } = {}) {
    this.store = store;
    this.bus = bus;
    this.document = documentRef;
    this.window = windowRef;
    this.fetch = fetchRef;
    this.onNavigate = onNavigate;
    this.root = null;
    this.messages = null;
    this.search = null;
    this.count = null;
    this.composer = null;
    this.sendButton = null;
    this.statusText = null;
    this.error = null;
    this.endButton = null;
    this.empty = null;
    this.query = '';
    this.sending = false;
    this.dirty = true;
    // id da mensagem -> {node, text, partial, query}: só refaz o cartão que mudou
    this.rendered = new Map();
    this.unsubscribeStore = null;
    this.unsubscribeView = null;
    this.onDocumentKeyDown = (event) => {
      if (this.root?.dataset.active !== 'true') return;
      if ((event.ctrlKey || event.metaKey) && String(event.key).toLowerCase() === 'f') {
        event.preventDefault();
        this.search?.focus?.();
        this.search?.select?.();
      } else if (event.key === 'Escape' && !isInputTarget(event.target)) {
        event.preventDefault();
        this.onNavigate?.('main');
      }
    };
  }

  mount(parent = this.document?.body) {
    if (!parent || this.root) return this.root;
    const doc = this.document;
    const root = create(doc, 'section', 'kui-chat-overlay');
    root.id = 'kui-chat';
    root.dataset.active = 'false';
    root.setAttribute('aria-label', 'Chat com a Kira');

    const card = create(doc, 'div', 'kui-chat-card');
    const header = create(doc, 'header', 'kui-chat-header');
    const badge = create(doc, 'div', 'kui-chat-badge');
    badge.appendChild(icon(doc, 'chat'));
    const titleGroup = create(doc, 'div', 'kui-chat-title-group');
    titleGroup.appendChild(create(doc, 'h2', 'kui-chat-title', 'Chat da Kira'));
    titleGroup.appendChild(create(doc, 'p', 'kui-chat-subtitle', 'A conversa desta sessão, por voz e por texto.'));
    const status = create(doc, 'div', 'kui-chat-runtime-status');
    status.appendChild(create(doc, 'span', 'kui-chat-status-dot'));
    this.statusText = create(doc, 'span', '', 'Pronta');
    status.appendChild(this.statusText);
    const actions = create(doc, 'div', 'kui-chat-actions');
    const mic = button(doc, 'kui-chat-chip', 'Microfone', 'mic', 'Ligar ou desligar microfone');
    mic.addEventListener('click', () => this.delegateControl('toggleMic'));
    const endSession = button(doc, 'kui-chat-chip is-danger', 'Encerrar sessão', 'power', 'Encerrar sessão');
    endSession.addEventListener('click', () => this.delegateControl('confirmDockDisconnect'));
    const close = button(doc, 'kui-chat-close', '', 'back', 'Voltar à tela principal');
    close.title = 'Voltar à tela principal (Esc)';
    close.addEventListener('click', () => this.onNavigate?.('main'));
    actions.appendChild(status);
    actions.appendChild(mic);
    actions.appendChild(endSession);
    actions.appendChild(close);
    header.appendChild(badge);
    header.appendChild(titleGroup);
    header.appendChild(actions);

    const tools = create(doc, 'div', 'kui-chat-tools');
    const searchBox = create(doc, 'label', 'kui-chat-search-box');
    searchBox.appendChild(icon(doc, 'search'));
    this.search = create(doc, 'input', 'kui-chat-search');
    this.search.type = 'search';
    this.search.placeholder = 'Buscar na conversa (Ctrl+F)';
    this.search.setAttribute('aria-label', 'Buscar na conversa');
    this.search.addEventListener('input', () => { this.query = this.search.value; this.render(); });
    searchBox.appendChild(this.search);
    this.count = create(doc, 'span', 'kui-chat-count', '');
    tools.appendChild(searchBox);
    tools.appendChild(this.count);

    this.messages = create(doc, 'div', 'kui-chat-messages');
    this.messages.setAttribute('role', 'log');
    this.messages.setAttribute('aria-live', 'polite');
    this.messages.addEventListener('scroll', () => { if (this.endButton) this.endButton.hidden = isAtEnd(this.messages); });
    this.empty = create(doc, 'div', 'kui-chat-empty');
    const emptyBadge = create(doc, 'div', 'kui-chat-badge');
    emptyBadge.appendChild(icon(doc, 'chat'));
    this.empty.appendChild(emptyBadge);
    this.empty.appendChild(create(doc, 'strong', '', 'A conversa está pronta'));
    this.empty.appendChild(create(doc, 'p', '', 'Envie uma mensagem ou fale com a Kira para começar.'));
    this.endButton = button(doc, 'kui-chat-go-end', 'Ir para o fim', 'down');
    this.endButton.hidden = true;
    this.endButton.addEventListener('click', () => this.scrollToEnd());

    const composer = create(doc, 'form', 'kui-chat-composer');
    this.composer = create(doc, 'textarea', 'kui-chat-input');
    this.composer.rows = 2;
    this.composer.placeholder = 'Escreva para a Kira…  (Enter envia, Shift+Enter quebra a linha)';
    this.composer.setAttribute('aria-label', 'Mensagem para a Kira');
    this.composer.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); this.send(); }
    });
    this.sendButton = button(doc, 'kui-chat-send', 'Enviar', 'send');
    this.sendButton.type = 'submit';
    composer.addEventListener('submit', (event) => { event.preventDefault(); this.send(); });
    composer.appendChild(this.composer);
    composer.appendChild(this.sendButton);
    this.error = create(doc, 'p', 'kui-chat-error');
    this.error.hidden = true;

    card.appendChild(header);
    card.appendChild(tools);
    card.appendChild(this.messages);
    card.appendChild(this.endButton);
    card.appendChild(this.error);
    card.appendChild(composer);
    root.appendChild(card);
    parent.appendChild(root);
    this.root = root;

    // escondida, só marca que mudou; redesenha ao abrir
    this.unsubscribeStore = this.store?.subscribe?.(() => {
      if (this.root?.dataset.active === 'true') this.render();
      else this.dirty = true;
    }) || null;
    this.unsubscribeView = this.bus?.on?.('view:change', ({ view }) => {
      root.dataset.active = view === 'chat' ? 'true' : 'false';
      this.setMainIsolation(view === 'chat');
      if (view === 'chat') {
        this.render();
        this.scrollToEnd();
        this.window?.setTimeout?.(() => this.composer?.focus?.(), 0);
      }
    }) || null;
    this.document?.addEventListener?.('keydown', this.onDocumentKeyDown);
    this.render();
    return root;
  }

  delegateControl(actionName) {
    try {
      const result = this.window?.[actionName]?.();
      if (result && typeof result.catch === 'function') result.catch(() => {});
    } catch (_) {}
  }

  setMainIsolation(active) {
    this.document?.body?.classList?.toggle?.('kui-chat-open', Boolean(active));
    // Chat por cima de tudo (estilo padrão): orbes param; no estilo "sobre o orbe" o orbe continua à mostra
    const behind = Boolean(active) && this.document?.documentElement?.dataset?.kiraChat !== 'orbe';
    try {
      (this.window?.kira3D || this.window?.__kira3dInstance)?.hold?.('chat', behind);
      const userPaused = this.window?.localStorage?.getItem?.('KIRA_ORB_PAUSED') === 'true';
      this.window?.KiraAppearance?.setPaused?.(behind || userPaused);
    } catch (_) { /* sem orbe */ }
    const appShell = this.document?.getElementById?.('app-shell');
    const uiRoot = this.document?.getElementById?.('kira-ui-root');
    [appShell, uiRoot].forEach((node) => {
      if (!node) return;
      if ('inert' in node) node.inert = Boolean(active);
      node.setAttribute('aria-hidden', String(Boolean(active)));
      node.style.pointerEvents = active ? 'none' : '';
    });
  }

  render() {
    if (!this.messages || !this.store) return;
    this.dirty = false;
    const state = this.store.getState();
    const wasAtEnd = isAtEnd(this.messages) || !this.rendered.size;
    const messages = state.session?.messages || [];
    const seen = new Set();
    let previous = null;
    messages.forEach((message, index) => {
      const id = String(message.id || `sem-id-${index}`);
      seen.add(id);
      let entry = this.rendered.get(id);
      const text = String(message.text || '');
      const partial = Boolean(message.partial);
      if (!entry) {
        entry = { node: this.createMessage(message), text: null, partial: null, query: null };
        this.rendered.set(id, entry);
      }
      if (entry.text !== text || entry.partial !== partial || entry.query !== this.query) {
        this.fillMessage(entry.node, message);
        Object.assign(entry, { text, partial, query: this.query });
      }
      // ordem certa sem mexer em quem já está no lugar
      const expected = previous ? previous.nextSibling : this.messages.firstChild;
      if (expected !== entry.node) this.messages.insertBefore(entry.node, expected || null);
      previous = entry.node;
    });
    for (const [id, entry] of this.rendered) {
      if (!seen.has(id)) {
        entry.node.remove?.();
        this.rendered.delete(id);
      }
    }
    if (!messages.length && this.empty.parentNode !== this.messages) this.messages.appendChild(this.empty);
    if (messages.length && this.empty.parentNode === this.messages) this.empty.remove?.();
    if (this.count) this.count.textContent = messages.length ? `${messages.length} ${messages.length === 1 ? 'mensagem' : 'mensagens'}` : '';
    const status = state.status || {};
    const activeStatus = status.state === 'thinking' || status.state === 'searching';
    this.statusText.textContent = activeStatus ? (status.state === 'searching' ? 'Pesquisando…' : 'Pensando…') : (status.text || (status.state === 'speaking' ? 'Falando' : 'Pronta'));
    this.root?.classList.toggle?.('is-busy', activeStatus);
    this.root?.classList.toggle?.('is-speaking', status.state === 'speaking');
    if (wasAtEnd) this.scrollToEnd();
    else if (this.endButton) this.endButton.hidden = false;
  }

  createMessage(message) {
    const doc = this.document;
    const role = message.role || 'system';
    const article = create(doc, 'article', `kui-chat-message is-${role}`);
    article.dataset.messageId = message.id || '';
    const meta = create(doc, 'div', 'kui-chat-message-meta');
    if (role === 'kira') {
      const avatar = create(doc, 'img', 'kui-chat-avatar');
      avatar.src = 'assets/ui/Kira_rosto.png';
      avatar.alt = 'Kira';
      meta.appendChild(avatar);
    }
    meta.appendChild(create(doc, 'span', 'kui-chat-author', role === 'user' ? 'Você' : role === 'kira' ? 'Kira' : 'Sistema'));
    const source = create(doc, 'span', 'kui-chat-source');
    const time = create(doc, 'span', 'kui-chat-time');
    meta.appendChild(source);
    meta.appendChild(time);
    const bubble = create(doc, 'div', 'kui-chat-bubble');
    const body = create(doc, 'div', 'kui-chat-text');
    const copy = button(doc, 'kui-chat-message-copy', '', 'copy', 'Copiar mensagem');
    copy.title = 'Copiar';
    copy.addEventListener('click', () => this.copy(article.__kiraText || '', copy));
    bubble.appendChild(body);
    bubble.appendChild(copy);
    article.appendChild(meta);
    article.appendChild(bubble);
    article.__kiraParts = { source, time, body };
    return article;
  }

  fillMessage(article, message) {
    const parts = article.__kiraParts;
    article.__kiraText = String(message.text || '');
    article.classList?.toggle?.('is-partial', Boolean(message.partial));
    parts.source.textContent = sourceLabel(message.source);
    parts.time.textContent = timeLabel(message.timestamp);
    renderMarkdown(parts.body, message.text, {
      documentRef: this.document,
      query: this.query,
      onCopy: (value, buttonNode) => this.copy(value, buttonNode),
    });
  }

  async copy(value, buttonNode) {
    // botão com texto (cópia de código) muda o texto; o de ícone muda a dica
    const label = buttonNode?.querySelector?.('span');
    const iconOnly = !label && Boolean(buttonNode?.querySelector?.('svg'));
    const say = (text) => {
      if (label) label.textContent = text;
      else if (iconOnly) buttonNode.title = text;
      else if (buttonNode) buttonNode.textContent = text;
    };
    try {
      if (this.window?.navigator?.clipboard?.writeText) await this.window.navigator.clipboard.writeText(String(value));
      else {
        const area = create(this.document, 'textarea');
        area.value = String(value);
        this.document.body.appendChild(area);
        area.select?.();
        this.document.execCommand?.('copy');
        area.remove?.();
      }
      buttonNode?.classList?.add?.('is-done');
      say('Copiado');
      this.window?.setTimeout?.(() => { buttonNode?.classList?.remove?.('is-done'); say('Copiar'); }, 1200);
    } catch (_) {
      say('Não copiado');
    }
  }

  async send() {
    const value = String(this.composer?.value || '').trim();
    if (!value || this.sending || typeof this.fetch !== 'function') return false;
    this.sending = true;
    this.composer.disabled = true;
    this.sendButton.disabled = true;
    this.error.hidden = true;
    try {
      await sendChatMessage(value, { source: 'text', input_type: 'text', message_type: 'text' }, {
        windowRef: this.window,
        fetchRef: this.fetch,
      });
      this.composer.value = '';
      return true;
    } catch (error) {
      this.error.textContent = `Não consegui enviar agora. ${error?.message || 'Tente novamente.'}`;
      this.error.hidden = false;
      return false;
    } finally {
      this.sending = false;
      this.composer.disabled = false;
      this.sendButton.disabled = false;
      this.composer.focus?.();
    }
  }

  scrollToEnd() {
    if (!this.messages) return;
    this.messages.scrollTop = this.messages.scrollHeight;
    if (this.endButton) this.endButton.hidden = true;
  }

  destroy() {
    this.unsubscribeStore?.();
    this.unsubscribeView?.();
    this.document?.removeEventListener?.('keydown', this.onDocumentKeyDown);
    this.root?.remove?.();
    this.setMainIsolation(false);
    this.rendered.clear();
    this.root = null;
  }
}
