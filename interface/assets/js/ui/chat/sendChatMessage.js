/**
 * Único transporte HTTP da conversa por texto.
 * `window.fetch(...)` é chamado como método para preservar o receptor exigido
 * pelo WebView/Chromium e evitar `Illegal invocation`.
 */
export async function sendChatMessage(text, extraPayload = {}, {
  windowRef = globalThis.window,
  fetchRef = null,
  onActivity = null,
  logger = globalThis.console,
} = {}) {
  const message = String(text == null ? '' : text).trim();
  if (!message) throw new Error('Mensagem vazia.');
  onActivity?.('text_input', { throttleMs: 0, fireAndForget: true });
  logger?.info?.('UI_TEXT_HTTP_FALLBACK_SEND_BEGIN text=%s', message);

  const doFetch = (...args) => {
    if (windowRef && typeof windowRef.fetch === 'function') return windowRef.fetch(...args);
    if (typeof fetchRef === 'function') return fetchRef(...args);
    if (typeof globalThis.fetch === 'function') return globalThis.fetch(...args);
    throw new Error('Transporte HTTP indisponível.');
  };

  let response;
  try {
    response = await doFetch('/api/chat/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: message, ...extraPayload }),
    });
  } catch (_) {
    throw new Error('Não consegui conectar ao backend.');
  }

  const raw = await response.text();
  let data = {};
  try { data = raw ? JSON.parse(raw) : {}; } catch (_) {}
  if (!response.ok) {
    throw new Error(String(data?.detail || raw || 'Falha ao enviar mensagem em modo texto.'));
  }
  logger?.info?.('UI_TEXT_HTTP_FALLBACK_SEND_OK');
  return data;
}

if (typeof window !== 'undefined') {
  window.KiraChatTransport = Object.freeze({ sendChatMessage });
}
