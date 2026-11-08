// Selo de privacidade do "onde está isso?": avisa enquanto a Kira captura a tela e diz o resultado por 2 s.

const TEXT = Object.freeze({
  looking: 'Olhando a tela',
  found: 'Destaquei na tela',
  ambiguous: 'Destaquei as opções',
  not_found: 'Não encontrei na tela',
  error: 'Não consegui olhar a tela',
});
const LOOKING_MAX_MS = 15000;
const RESULT_MS = 2000;

export function createScreenBadge({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  const badge = documentRef.createElement('div');
  badge.className = 'kui-screen-badge';
  badge.hidden = true;
  badge.setAttribute('role', 'status');
  badge.setAttribute('aria-live', 'polite');
  documentRef.body.appendChild(badge);
  let timer = null;

  function show(action) {
    windowRef.clearTimeout(timer);
    const text = TEXT[action?.state];
    if (!text) { badge.hidden = true; return; }
    badge.textContent = text;
    badge.dataset.state = action.state;
    badge.hidden = false;
    timer = windowRef.setTimeout(() => { badge.hidden = true; }, action.state === 'looking' ? LOOKING_MAX_MS : RESULT_MS);
  }

  return { element: badge, show, destroy() { windowRef.clearTimeout(timer); badge.remove(); } };
}
