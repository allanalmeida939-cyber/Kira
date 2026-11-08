export const MAIN_WINDOW_ID = 'w1';
// Tela aberta pelo celular: vê o que é da principal e o que foi aberto para ela (Chefe usando o celular).
export const PHONE_WINDOW_ID = 'celular';

/** Ação sem windowId pertence à janela principal; mudanças de conteúdo ('window') são só dos satélites. */
export function isForWindow(action, windowId = MAIN_WINDOW_ID) {
  if (!action || typeof action !== 'object') return false;
  if (action.type === 'window') return windowId !== MAIN_WINDOW_ID && action.windowId === windowId;
  if (windowId === PHONE_WINDOW_ID) return !action.windowId || action.windowId === PHONE_WINDOW_ID;
  return (action.windowId || MAIN_WINDOW_ID) === windowId;
}

/** Filtra uma ação ou lote (array) para a janela; devolve null quando nada sobra. */
export function filterForWindow(raw, windowId = MAIN_WINDOW_ID) {
  if (Array.isArray(raw)) {
    const kept = raw.filter((item) => isForWindow(item, windowId));
    return kept.length ? kept : null;
  }
  return isForWindow(raw, windowId) ? raw : null;
}
