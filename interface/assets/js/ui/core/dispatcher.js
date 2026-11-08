import { parseAction } from './actions.js';

const DEFAULT_LIMITS = Object.freeze({ maxKiraPanels: 5, maxActionsPerSecond: 10 });

function isTyping() {
  const active = globalThis.document?.activeElement;
  return Boolean(active?.matches?.('input, textarea, select, [contenteditable="true"]'));
}

/**
 * @param {{windowManager: object, bus: object, store?: object, limits?: object, logger?: Console}} options
 */
export function createDispatcher({ windowManager, bus, store = null, limits = {}, logger = globalThis.console } = {}) {
  if (!windowManager || typeof windowManager.open !== 'function') throw new TypeError('dispatcher precisa de windowManager.');
  const config = { ...DEFAULT_LIMITS, ...limits };
  const timestamps = [];

  function warn(message, value) {
    try { logger?.warn?.(`[Kira UI] ${message}`, value || ''); } catch (_) { /* best-effort */ }
  }
  function allowedRate() {
    const now = Date.now();
    while (timestamps.length && now - timestamps[0] >= 1000) timestamps.shift();
    if (timestamps.length >= config.maxActionsPerSecond) return false;
    timestamps.push(now);
    return true;
  }
  function shouldFocus(action) {
    return action.focus !== false && !isTyping();
  }
  function emit(action) {
    bus?.emit?.('action', action);
    bus?.emit?.(`action:${action.type}`, action);
  }

  function makeRoom() {
    if (!(windowManager.countByCreator?.('kira') >= config.maxKiraPanels)) return true;
    const oldest = windowManager.oldestByCreator?.('kira', (panel) => !panel.pinned && !panel.userTouched);
    if (oldest && windowManager.close(oldest.id)) return true;
    warn('limite de painéis da Kira atingido; nenhum painel elegível para fechar.');
    return false;
  }

  function dispatch(raw) {
    const action = parseAction(raw);
    if (!action) { warn('ação inválida ignorada.', raw); return false; }
    if (!allowedRate()) { warn('rate limit de ações excedido; ação descartada.', action.type); return false; }

    try {
      if (action.type === 'status') {
        store?.setStatus?.(action);
        emit(action);
        return true;
      }
      if (action.type === 'present' || action.type === 'present_patch') {
        const id = action.presentationId;
        if (windowManager.get?.(id)) {
          windowManager.update(id, { presentation: action });
          // apresentação nova no mesmo painel volta ao destaque (as outras viram miniatura ao lado)
          if (action.type === 'present') windowManager.feature?.(id);
          emit(action);
          return true;
        }
        if (action.type === 'present_patch' || !makeRoom()) return false;
        const opened = windowManager.open('presentation', { id, title: action.title, props: { presentation: action }, createdBy: 'kira', focus: !isTyping() });
        if (!opened) return false;
        store?.addPanel?.(opened.id);
        emit(action);
        return true;
      }
      if (action.type === 'open_panel') {
        const existing = action.id ? windowManager.get?.(action.id) : null;
        if (existing) {
          windowManager.update(action.id, action.props || {});
          if (shouldFocus(action)) { windowManager.feature?.(action.id); windowManager.focus(action.id); }
          emit(action);
          return true;
        }
        if (!makeRoom()) return false;
        const opened = windowManager.open(action.kind, {
          id: action.id,
          title: action.title,
          props: action.props,
          createdBy: 'kira',
          focus: shouldFocus(action),
          autoCloseMs: action.autoCloseMs,
          view: action.view,
        });
        if (!opened) return false;
        if (typeof windowManager.placeMediaPanels === 'function' && ['gallery', 'video', 'article', 'search-log', 'scoreboard', 'viewer', 'election', 'info', 'download', 'creation'].includes(action.kind)) windowManager.placeMediaPanels();
        store?.addPanel?.(opened.id);
        emit(action);
        return true;
      }
      if (action.type === 'update_panel') {
        const result = windowManager.update(action.id, action.props);
        if (!result) return false;
        emit(action);
        return true;
      }
      if (action.type === 'close_panel') {
        const existing = windowManager.get?.(action.id);
        if (action.preserveUserTouched && (existing?.pinned || existing?.userTouched)) {
          emit(action);
          return true;
        }
        const result = windowManager.close(action.id);
        if (result) store?.removePanel?.(action.id);
        emit(action);
        return Boolean(result);
      }
      if (action.type === 'ui_control') {
        const control = globalThis.KiraUiControl;
        const result = action.command === 'open_tab' ? control?.openTab?.(action.value) : control?.musicCommand?.(action.command, action.value);
        emit(action);
        return Boolean(result?.ok);
      }
      if (action.type === 'ui_call') {
        // Olho de Deus no celular: executa e devolve o resultado ao backend (services/phone_rpc.py)
        const targets = { godseye: globalThis.KiraGodsEye, rota: globalThis.KiraRouteOverlay };
        let result = null;
        try { result = targets[action.target]?.[action.method]?.(action.args) ?? null; } catch (_) { result = null; }
        globalThis.fetch?.('/api/phone/ui-result', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: action.id, result: result && typeof result === 'object' ? result : { ok: Boolean(result) } }) }).catch(() => {});
        emit(action);
        return true;
      }
      if (action.type === 'media_cue' || action.type === 'screen_locate_result') {
        emit(action);
        return true;
      }
      if (action.type === 'layout') {
        if (typeof windowManager.applyLayout === 'function') windowManager.applyLayout(action.preset);
        emit(action);
        return true;
      }
    } catch (error) {
      warn('falha isolada ao despachar ação.', error);
      return false;
    }
    return false;
  }

  return Object.freeze({ dispatch, limits: Object.freeze(config) });
}
