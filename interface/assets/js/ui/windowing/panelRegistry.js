function safeText(value, fallback = '') {
  const text = String(value == null ? '' : value).trim();
  return text || fallback;
}

import { RemindersView } from '../views/RemindersView.js';
import { createArticlePanel } from '../views/ArticlePanel.js';
import { createGalleryPanel } from '../views/GalleryPanel.js';
import { createLogsPanel } from '../views/LogsPanel.js';
import { createSearchLogPanel } from '../views/SearchLogPanel.js';
import { createScoreboardPanel } from '../views/ScoreboardPanel.js';
import { createViewerPanel } from '../views/ViewerPanel.js';
import { createElectionPanel } from '../views/ElectionPanel.js';
import { createInfoPanel } from '../views/InfoPanel.js';
import { createDownloadPanel } from '../views/DownloadPanel.js';
import { createCreationPanel } from '../views/CreationPanel.js';
import { createVideoPanel } from '../views/VideoPanel.js';
import { createPresentationPanel } from '../views/PresentationPanel.js';
import { createSmartHomePanel } from '../views/SmartHomePanel.js';
import { createTutorPanel } from '../views/TutorPanel.js';
import { createQuadroPanel } from '../views/QuadroPanel.js';

/**
 * Registry de conteúdo. O gerenciador conhece a janela; o registry conhece o painel.
 */
export function createPanelRegistry({ logger = globalThis.console } = {}) {
  const definitions = new Map();

  function register(kind, definition) {
    const name = safeText(kind).toLowerCase();
    if (!name || !definition || typeof definition.mount !== 'function') {
      throw new TypeError('register exige kind e mount().');
    }
    definitions.set(name, {
      mount: definition.mount,
      update: typeof definition.update === 'function' ? definition.update : null,
      unmount: typeof definition.unmount === 'function' ? definition.unmount : null,
      visibilityChange: typeof definition.visibilityChange === 'function' ? definition.visibilityChange : null,
    });
    return registry;
  }

  function get(kind) { return definitions.get(safeText(kind).toLowerCase()) || null; }
  function has(kind) { return definitions.has(safeText(kind).toLowerCase()); }
  function unregister(kind) { return definitions.delete(safeText(kind).toLowerCase()); }
  function kinds() { return [...definitions.keys()]; }

  const registry = Object.freeze({ register, get, has, unregister, kinds });

  register('logs', createLogsPanel());

  register('reminders', {
    mount(container) {
      const view = new RemindersView({ documentRef: globalThis.document, windowRef: globalThis.window });
      return view.mount(container, { bus: null });
    },
  });

  register('gallery', createGalleryPanel());
  register('video', createVideoPanel());
  register('article', createArticlePanel());
  register('search-log', createSearchLogPanel());
  register('scoreboard', createScoreboardPanel());
  register('viewer', createViewerPanel());
  register('election', createElectionPanel());
  register('info', createInfoPanel());
  register('download', createDownloadPanel());
  register('tutor', createTutorPanel());
  register('quadro', createQuadroPanel()); // painéis que a Kira cria na hora
  register('creation', createCreationPanel());
  register('presentation', createPresentationPanel());
  register('smart_home', createSmartHomePanel());

  try { logger?.debug?.('[Kira UI] painel de teste logs registrado.'); } catch (_) { /* best-effort */ }
  return registry;
}

export const panelRegistry = createPanelRegistry;
