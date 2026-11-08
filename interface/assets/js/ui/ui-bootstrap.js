import { createEventBus } from './core/eventBus.js';
import { PHONE_WINDOW_ID, filterForWindow } from './core/windowFilter.js';
import { createStore } from './core/store.js';
import { createDispatcher } from './core/dispatcher.js';
import { MockTransport, WsTransport } from './core/transport.js';
import { createPanelRegistry } from './windowing/panelRegistry.js';
import { WindowManager } from './windowing/WindowManager.js';
import { ViewRouter } from './shell/ViewRouter.js';
import { Sidebar } from './shell/Sidebar.js';
import { ChatView } from './views/ChatView.js';
import { createStudiesWorkspace } from './views/StudiesWorkspace.js';
import { createReadinessBadgeController } from './readinessBadge.js';
import { StatusBar } from './shell/StatusBar.js';
import { installPanelDrag } from './shell/panelDrag.js';
import { createScreenBadge } from './shell/screenBadge.js';
import './vision/screenCapture.js';

function enabledByFlag() {
  // Celular (página marcada pelo portão): a camada de painéis vem sempre; lá não há o "?kiraui=1" da janela do PC.
  if (globalThis.KIRA_REMOTE) return true;
  try {
    const query = new URLSearchParams(globalThis.location?.search || '');
    const queryFlag = query.get('kiraui');
    const desktopFlag = query.get('kira.desktop') === '1';
    const storedFlag = globalThis.localStorage?.getItem('kira.ui.enabled');
    if (queryFlag === '0' || (desktopFlag && storedFlag === '0')) return false;
    if (queryFlag === '1') {
      try { globalThis.localStorage?.setItem('kira.ui.enabled', '1'); } catch (_) {}
      return true;
    }
    if (storedFlag === '0') return false;
    return storedFlag === '1';
  } catch (_) {
    return false;
  }
}

function bootstrap() {
  if (!enabledByFlag()) return;
  const root = document.getElementById('kira-ui-root');
  if (!root) { console?.warn?.('[Kira UI] root não encontrado; camada ignorada.'); return; }

  const bus = createEventBus();
  const store = createStore({ eventBus: bus, windowRef: globalThis.window, documentRef: globalThis.document });
  const registry = createPanelRegistry();
  const windowManager = new WindowManager(root, { registry, bus, store });
  const dispatcher = createDispatcher({ windowManager, bus, store });
  const reportPanel = (state) => (panel) => {
    if (!panel?.id) return;
    try {
      globalThis.fetch?.('/api/ui/media/panels/state', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: panel.id, state }) })?.catch?.(() => {});
    } catch (_) { /* o backend só usa isso para não fechar painel tocado; falha é inofensiva */ }
  };
  [['window:close', 'closed'], ['window:touch', 'touched'], ['window:pin', 'pinned'], ['window:unpin', 'unpinned']].forEach(([event, state]) => bus.on(event, reportPanel(state)));
  // painel solto: se sob o ponto houver outra janela da Kira (ao lado ou por cima), o backend passa o painel para ela
  bus.on('window:drop', (drop) => {
    globalThis.fetch?.('/api/ui/windows/transfer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ panel_id: drop.id, screen_x: drop.screenX, screen_y: drop.screenY }) })?.catch?.(() => {});
  });
  bus.on('action:close_panel', (action) => { if (action?.id === 'vitals') globalThis.hideKiraPanel?.('system_vitals'); });
  const viewRouter = new ViewRouter({ bus, store, windowManager, documentRef: globalThis.document });
  const chatView = new ChatView({ store, bus, documentRef: globalThis.document, windowRef: globalThis.window, onNavigate: (view) => viewRouter.go(view) });
  chatView.mount(globalThis.document.body);
  const screenBadge = createScreenBadge({ documentRef: globalThis.document, windowRef: globalThis.window });
  const studiesWorkspace = createStudiesWorkspace({ documentRef: globalThis.document, windowRef: globalThis.window });
  studiesWorkspace.mount(globalThis.document.body);
  globalThis.KiraStudies = studiesWorkspace;
  // Modo do atalho do microfone: em "segurar para falar" a sessão começa com o microfone desligado.
  globalThis.fetch?.('/api/ui/settings', { cache: 'no-store' })
    .then((response) => (response.ok ? response.json() : null))
    .then((body) => { globalThis.__kiraMicMode = body?.settings?.shortcuts?.mic_mode || 'toggle'; })
    .catch(() => {});
  bus.on('action:screen_locate_result', screenBadge.show);
  const readinessBadge = globalThis.document.createElement('div');
  readinessBadge.className = 'kui-readiness-badge';
  readinessBadge.hidden = true;
  readinessBadge.setAttribute('role', 'status');
  readinessBadge.setAttribute('aria-live', 'polite');
  globalThis.document.body.appendChild(readinessBadge);
  const readinessController = createReadinessBadgeController(readinessBadge, {
    onRestart: () => { globalThis.fetch?.('/api/system/restart', { method: 'POST' }).catch(() => {}); },
  });
  const syncReadinessBadge = (snapshot) => readinessController.sync(snapshot);
  const panelDrag = installPanelDrag({ documentRef: globalThis.document, windowRef: globalThis.window });
  globalThis.KiraDispatch = (action) => dispatcher.dispatch(action); // a vitrine da aba Música abre vídeos no painel
  globalThis.KiraPanelLayout = panelDrag; // a tool "painel" da Kira move e redimensiona por aqui (evento ui_panel_layout)
  const sidebar = new Sidebar({ router: viewRouter, dispatcher, windowManager, documentRef: globalThis.document, windowRef: globalThis.window });
  globalThis.KiraSidebar = sidebar; // openTab('casa') abre o painel Casa
  sidebar.mount(globalThis.document.body);
  const statusBar = new StatusBar({ bus, documentRef: globalThis.document, windowRef: globalThis.window });
  statusBar.mount(globalThis.document.body);
  const onRuntimeStatus = (event) => {
    const snapshot = event?.detail || {};
    store.setRuntime(snapshot);
    syncReadinessBadge(snapshot);
    const raw = String(snapshot.state || snapshot.phase || snapshot.voice_status || '').toLowerCase();
    const state = raw.includes('search') ? 'searching' : raw.includes('think') ? 'thinking' : raw.includes('listen') ? 'listening' : raw.includes('speak') ? 'speaking' : 'idle';
    store.setStatus({ state, text: String(snapshot.ui_message || snapshot.message || '') });
  };
  globalThis.document.addEventListener('kira:runtime-status', onRuntimeStatus);
  globalThis.fetch?.('/api/runtime-status')
    .then((response) => (response.ok ? response.json() : null))
    .then((snapshot) => { if (snapshot) { store.setRuntime(snapshot); syncReadinessBadge(snapshot); } })
    .catch(() => {});
  const transport = new WsTransport();
  const windowId = globalThis.KIRA_REMOTE ? PHONE_WINDOW_ID : undefined;
  transport.start((raw) => { const mine = filterForWindow(raw, windowId); if (mine) dispatcher.dispatch(mine); });
  const demoTransport = new MockTransport();
  demoTransport.start((raw) => dispatcher.dispatch(raw));
  root.hidden = false;
  root.removeAttribute('hidden');

  const api = {
    version: 1,
    bus,
    store,
    registry,
    windowManager,
    viewRouter,
    sidebar,
    chatView,
    studiesWorkspace,
    dispatcher,
    transport,
    readinessBadge,
    dispatch: (raw) => dispatcher.dispatch(raw),
    demo: () => demoTransport.demo(),
    destroy: () => {
      transport.stop();
      demoTransport.stop();
      globalThis.document.removeEventListener('kira:runtime-status', onRuntimeStatus);
      sidebar.destroy();
      panelDrag.destroy();
      chatView.destroy();
      readinessController.destroy();
      screenBadge.destroy();
      studiesWorkspace.destroy();
      readinessBadge.remove();
      statusBar.destroy();
      store.destroy();
      windowManager.destroy();
      try { delete globalThis.KiraUI; } catch (_) { globalThis.KiraUI = undefined; }
      try { delete globalThis.KiraStudies; } catch (_) { globalThis.KiraStudies = undefined; }
    },
  };
  globalThis.KiraUI = api;
  bus.emit('ui:ready', { version: api.version });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootstrap, { once: true });
else bootstrap();
