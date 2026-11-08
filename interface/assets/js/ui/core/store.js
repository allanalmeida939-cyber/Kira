const VIEW_IDS = Object.freeze(['main', 'chat']);
const KIRA_STATES = Object.freeze(['idle', 'listening', 'thinking', 'searching', 'speaking']);
const DEFAULT_STATUS = Object.freeze({ state: 'idle', text: '', steps: [] });

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function text(value) {
  return String(value == null ? '' : value).trim();
}

function normalizeRole(detail = {}) {
  const value = text(detail.role || detail.speaker || detail.sender || '').toLowerCase();
  if (['user', 'usuario', 'voce', 'você', 'you'].includes(value)) return 'user';
  if (['assistant', 'agent', 'kira', 'kira-agent'].includes(value)) return 'kira';
  return 'system';
}

function normalizeSource(detail = {}) {
  const value = text(detail.source || detail.message_type || detail.messageType || '').toLowerCase();
  if (/voice|voz|mic|microphone|speech|audio|realtime|livekit|transcript_final/.test(value)) return 'voice';
  if (/text|texto|typed|api_chat_send|data_channel/.test(value)) return 'text';
  return value || 'system';
}

export function normalizeTranscriptDetail(detail = {}) {
  const messageText = text(detail.text || detail.message || detail.content);
  if (!messageText) return null;
  const partial = Boolean(detail.partial ?? detail.streaming ?? detail.is_partial ?? (detail.is_final === false));
  const id = text(detail.id || detail.message_id || detail.messageId || detail.reply_id || detail.replyId || detail.request_id || detail.requestId);
  const turnId = text(detail.turn_id || detail.turnId || detail.parent_turn_id || detail.parentTurnId);
  const timestampValue = Number(detail.timestamp || detail.created_at || detail.createdAt || Date.now());
  return {
    id: id || null,
    turnId: turnId || null,
    role: normalizeRole(detail),
    source: normalizeSource(detail),
    text: messageText,
    timestamp: Number.isFinite(timestampValue) ? timestampValue : Date.now(),
    partial,
  };
}

function hydrateMessages(documentRef) {
  const container = documentRef?.getElementById?.('left-transcription-messages');
  if (!container?.querySelectorAll) return [];
  const messages = [];
  container.querySelectorAll('.left-transcription-message').forEach((node, index) => {
    const author = text(node.querySelector?.('.left-transcription-author')?.textContent).replace(/:.*$/, '').toLowerCase();
    const messageText = text(node.querySelector?.('.left-transcription-text')?.textContent || node.textContent);
    const badge = text(node.querySelector?.('.left-transcription-source-badge')?.textContent).toLowerCase();
    if (!messageText) return;
    messages.push({
      id: `dom-${index + 1}`,
      turnId: null,
      role: ['kira', 'assistant'].includes(author) ? 'kira' : ['voce', 'você', 'user'].includes(author) ? 'user' : 'system',
      source: badge === 'voz' || badge === 'voice' ? 'voice' : badge === 'texto' || badge === 'text' ? 'text' : 'system',
      text: messageText,
      timestamp: Date.now() + index,
      partial: false,
      hydrated: true,
    });
  });
  return messages;
}

/**
 * The existing /ws/transcription handler remains the single transport owner.
 * The optional window listener only receives its normalized CustomEvent bridge.
 */
export function createStore({ eventBus = null, windowRef = globalThis.window, documentRef = globalThis.document } = {}) {
  let state = {
    view: 'main',
    status: clone(DEFAULT_STATUS),
    mediaStatus: clone(DEFAULT_STATUS),
    panelIds: [],
    runtime: {},
    session: { messages: hydrateMessages(documentRef) },
  };
  const subscribers = new Set();
  const recentMessages = new Map();

  function publish(reason, detail = null) {
    const snapshot = getState();
    subscribers.forEach((listener) => {
      try { listener(snapshot, reason, detail); } catch (error) { console?.error?.('[Kira UI] store listener falhou', error); }
    });
    eventBus?.emit?.('store:change', { state: snapshot, reason, detail });
    return snapshot;
  }

  function getState() { return clone(state); }
  function subscribe(listener) {
    if (typeof listener !== 'function') throw new TypeError('store.subscribe exige uma função.');
    subscribers.add(listener);
    return () => subscribers.delete(listener);
  }
  function setView(view) {
    if (!VIEW_IDS.includes(view)) return false;
    if (state.view === view) return true;
    state = { ...state, view };
    publish('view');
    return true;
  }
  function setStatus(status) {
    if (!status || typeof status !== 'object' || !KIRA_STATES.includes(status.state)) return false;
    const nextStatus = { state: status.state, text: text(status.text), steps: Array.isArray(status.steps) ? clone(status.steps) : [] };
    state = status.scope === 'media'
      ? { ...state, mediaStatus: nextStatus }
      : { ...state, status: nextStatus };
    publish(status.scope === 'media' ? 'media:status' : 'status');
    return true;
  }
  function setRuntime(snapshot) {
    if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) return false;
    state = { ...state, runtime: clone(snapshot) };
    publish('runtime');
    return getState();
  }
  function addPanel(id) {
    if (!state.panelIds.includes(id)) {
      state = { ...state, panelIds: [...state.panelIds, id] };
      publish('panel:add');
    }
    return getState();
  }
  function removePanel(id) {
    const panelIds = state.panelIds.filter((panelId) => panelId !== id);
    if (panelIds.length !== state.panelIds.length) {
      state = { ...state, panelIds };
      publish('panel:remove');
    }
    return getState();
  }
  function addTranscript(detail) {
    const message = normalizeTranscriptDetail(detail);
    if (!message) return null;
    const identity = message.id || (message.partial ? `partial:${message.role}:${message.source}` : null);
    if (identity) {
      const existingIndex = state.session.messages.findIndex((item) => item.id === identity || (message.turnId && item.turnId === message.turnId && item.role === message.role && item.partial));
      if (existingIndex >= 0) {
        const next = [...state.session.messages];
        next[existingIndex] = { ...next[existingIndex], ...message, id: next[existingIndex].id || identity };
        state = { ...state, session: { ...state.session, messages: next } };
        publish('transcript:update', next[existingIndex]);
        return next[existingIndex];
      }
    }
    const dedupeKey = `${message.role}|${message.source}|${message.text}`;
    const lastAt = recentMessages.get(dedupeKey) || 0;
    if (!identity && Date.now() - lastAt < 2200) return null;
    recentMessages.set(dedupeKey, Date.now());
    const nextMessage = { ...message, id: identity || `msg-${Date.now()}-${state.session.messages.length + 1}` };
    state = { ...state, session: { ...state.session, messages: [...state.session.messages, nextMessage] } };
    publish('transcript:add', nextMessage);
    eventBus?.emit?.('transcript', nextMessage);
    return nextMessage;
  }
  function clearSession() {
    state = { ...state, session: { messages: [] }, panelIds: [] };
    recentMessages.clear();
    publish('session:clear');
    return getState();
  }
  function getTranscriptSource() {
    return Object.freeze({
      type: 'custom-event-bridge',
      endpoint: '/ws/transcription',
      owner: 'interface/index.html',
      duplicate: false,
      note: 'O histórico é alimentado exclusivamente por kira:transcript; o store não abre WebSocket.',
    });
  }

  const onTranscript = (event) => addTranscript(event?.detail);
  windowRef?.addEventListener?.('kira:transcript', onTranscript);

  function destroy() {
    windowRef?.removeEventListener?.('kira:transcript', onTranscript);
    subscribers.clear();
  }

  return Object.freeze({ getState, subscribe, setView, setStatus, setRuntime, addPanel, removePanel, addTranscript, clearSession, getTranscriptSource, destroy });
}
