import { createEventBus } from './core/eventBus.js';
import { createDispatcher } from './core/dispatcher.js';
import { filterForWindow } from './core/windowFilter.js';
import { createPanelRegistry } from './windowing/panelRegistry.js';
import { WindowManager } from './windowing/WindowManager.js';
import { catalog, widgetFor, matches, variantCount, CATEGORIES } from './widgets/catalog.js';
import { createStatsStream } from './widgets/statsStream.js';
import { el, clear } from './widgets/kit.js';
import { clampToViewport } from './windowing/placement.js';
import { loadMotionSettings } from './shell/motionSettings.js';

const WINDOW_ID_RE = /^[a-z0-9_-]{1,32}$/;
const SAVE_DELAY_MS = 500;
const MIN_W = 120;
const MIN_H = 80;
const DRAG_THRESHOLD = 5;
const INTERACTIVE = 'input, textarea, select, button, a, label, [contenteditable="true"], .sat-options';
const PUZZLE = 'M10 3a2 2 0 0 1 4 0v2h4a1 1 0 0 1 1 1v4h-2a2 2 0 0 0 0 4h2v4a1 1 0 0 1-1 1h-4v-2a2 2 0 0 0-4 0v2H6a1 1 0 0 1-1-1v-4h2a2 2 0 0 0 0-4H5V6a1 1 0 0 1 1-1h4z';
const LOCK = 'M7 11V8a5 5 0 0 1 10 0v3M5 11h14v10H5z';
const SVG_NS = 'http://www.w3.org/2000/svg';

export function windowIdFromLocation(search = globalThis.location?.search || '') {
  const query = new URLSearchParams(search);
  const id = query.get('window') || '';
  return query.get('role') === 'satellite' && WINDOW_ID_RE.test(id) && id !== 'w1' ? id : null;
}

/** Moldura padrão: widgets "sem container" por definição nascem só com o conteúdo sobre o fundo. */
export const isFramed = (widget, definition) => (typeof widget.frame === 'boolean' ? widget.frame : !definition.bare);

function icon(path) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linejoin', 'round');
  const node = document.createElementNS(SVG_NS, 'path');
  node.setAttribute('d', path);
  svg.appendChild(node);
  return svg;
}

function iconButton(path, label, className) {
  const button = el('button', `sat-icon-button ${className}`);
  button.type = 'button';
  button.title = label;
  button.setAttribute('aria-label', label);
  button.appendChild(icon(path));
  return button;
}

function bootstrap() {
  loadMotionSettings(); // mesmas animações da janela principal (aba Aparência)
  const windowId = windowIdFromLocation();
  const stage = document.getElementById('sat-stage');
  if (!windowId || !stage) return;
  // Flutuante (Kira minimizada/na bandeja): monitor inteiro transparente, só os painéis aparecem.
  if (new URLSearchParams(location.search).get('float') === '1') {
    document.documentElement.classList.add('sat-float');
    let had = false;
    new MutationObserver(() => {
      if (document.querySelector('.kui-window:not(.is-closing)')) had = true;
      else if (had) fetch(`/api/ui/windows/${windowId}`, { method: 'DELETE' }).catch(() => {});
    }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
    let last = '';
    setInterval(() => { // onde estão os painéis: fora deles o clique vai para a área de trabalho (kira_app click_through)
      const rects = JSON.stringify([...document.querySelectorAll('.kui-window:not(.is-closing)')].map((e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom].map(Math.round); }));
      if (rects !== last) fetch(`/api/ui/windows/${windowId}/hit`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: (last = rects) }).catch(() => {});
    }, 250);
  }
  const bus = createEventBus();
  const stats = createStatsStream();
  const transcriptBuffer = [];
  let lastRuntime = null;
  let widgets = [];
  let locked = false;
  let saveTimer = null;
  const instances = new Map();

  const on = (event, handler) => bus.on(event, handler);

  // ---------- persistência ----------
  const save = () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      fetch(`/api/ui/windows/${windowId}/layout`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ widgets, locked }) }).catch(() => {});
    }, SAVE_DELAY_MS);
  };

  const openExternal = (url) => { fetch('/api/ui/open-external', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) }).catch(() => {}); };

  // ---------- montagem de um widget (usada no layout e nas prévias) ----------
  function runWidget(definition, widget, body, { preview = false } = {}) {
    const timers = [];
    const ctx = {
      options: widget.options || {},
      style: widget.style || '',
      windowId,
      preview,
      stats,
      transcriptBuffer,
      get lastRuntime() { return lastRuntime; },
      on,
      openExternal,
      /** Só roda quando a janela está visível: janela oculta ou minimizada não consulta nada. */
      every(ms, fn) { timers.push(setInterval(() => { if (!document.hidden) fn(); }, ms)); },
      setOptions(patch) { widget.options = { ...(widget.options || {}), ...patch }; ctx.options = widget.options; if (!preview) save(); },
    };
    let cleanup = null;
    try {
      cleanup = definition.mount(body, ctx);
    } catch (_) {
      body.appendChild(el('p', 'w-muted w-center', 'indisponível'));
    }
    return () => {
      timers.forEach((timer) => clearInterval(timer));
      try { if (typeof cleanup === 'function') cleanup(); } catch (_) { /* widget já descartado */ }
    };
  }

  // ---------- widgets no layout ----------
  const stageSize = () => ({ width: stage.clientWidth || window.innerWidth, height: stage.clientHeight || window.innerHeight });

  function freeSpot(size) {
    const { width, height } = stageSize();
    const rects = widgets.map((w) => ({ x: w.x || 0, y: w.y || 0, w: w.w || 0, h: w.h || 0 }));
    for (let y = 16; y + size.h <= height; y += 24) {
      for (let x = 16; x + size.w <= width; x += 24) {
        if (!rects.some((r) => x < r.x + r.w + 8 && x + size.w + 8 > r.x && y < r.y + r.h + 8 && y + size.h + 8 > r.y)) return { x, y };
      }
    }
    return { x: 16 + (widgets.length % 8) * 24, y: 16 + (widgets.length % 8) * 24 };
  }

  function unmount(id) {
    const instance = instances.get(id);
    if (!instance) return;
    instance.stop?.();
    instance.node.remove();
    instances.delete(id);
  }

  function mountBody(widget, definition, instance) {
    instance.stop?.();
    clear(instance.body);
    instance.body.className = 'sat-body-content';
    instance.stop = runWidget(definition, widget, instance.body);
  }

  // Layout salvo numa janela maior (ou widget maior que a satélite) não pode ficar cortado: a exibição encaixa no
  // palco atual e o valor salvo só muda quando o usuário mexe no widget.
  const visibleRect = (widget) => clampToViewport({ x: widget.x, y: widget.y, width: widget.w, height: widget.h }, stageSize(), { margin: 0, minWidth: MIN_W, minHeight: MIN_H });

  function applyRect(widget, node) {
    const rect = visibleRect(widget);
    Object.assign(node.style, { left: `${rect.x}px`, top: `${rect.y}px`, width: `${rect.width}px`, height: `${rect.height}px` });
  }

  function applyFrame(widget, definition, node) {
    const framed = isFramed(widget, definition);
    node.classList.toggle('is-framed', framed);
    node.classList.toggle('is-bare', !framed);
    node.querySelector('.sat-widget-head')?.toggleAttribute('hidden', !framed);
  }

  /** Arrasta ou redimensiona; no modo sem moldura o arraste só começa depois de 5 px de movimento. */
  function track(event, widget, node, mode) {
    if (locked) return;
    const start = { x: event.clientX, y: event.clientY, left: widget.x, top: widget.y, w: widget.w, h: widget.h };
    let moving = mode !== 'drag-bare';
    if (moving) event.preventDefault();
    const move = (e) => {
      const dx = e.clientX - start.x;
      const dy = e.clientY - start.y;
      if (!moving) {
        if (Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
        moving = true;
        node.classList.add('is-dragging');
      }
      if (mode === 'drag' || mode === 'drag-bare') { widget.x = Math.round(start.left + dx); widget.y = Math.round(start.top + dy); }
      if (mode === 'se' || mode === 'e') widget.w = Math.max(MIN_W, Math.round(start.w + dx));
      if (mode === 'se' || mode === 's') widget.h = Math.max(MIN_H, Math.round(start.h + dy));
      applyRect(widget, node);
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      node.classList.remove('is-dragging');
      if (!moving) return;
      const rect = visibleRect(widget);
      Object.assign(widget, { x: rect.x, y: rect.y, w: rect.width, h: rect.height });
      save();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  function field(labelText, input) {
    const label = el('label', 'sat-field');
    label.append(el('span', '', labelText), input);
    return label;
  }

  function optionsMenu(widget, definition, instance) {
    const old = instance.node.querySelector('.sat-options');
    if (old) { old.remove(); return; }
    const form = el('div', 'sat-options');
    const changed = () => { applyFrame(widget, definition, instance.node); mountBody(widget, definition, instance); save(); };
    if (definition.styles) {
      const select = el('select', 'w-input');
      definition.styles.forEach(([value, label]) => { const option = el('option', '', label); option.value = value; select.appendChild(option); });
      select.value = widget.style || definition.styles[0][0];
      select.addEventListener('change', () => { widget.style = select.value; changed(); });
      form.appendChild(field('Estilo', select));
    }
    const frame = el('input');
    frame.type = 'checkbox';
    frame.checked = isFramed(widget, definition);
    frame.addEventListener('change', () => { widget.frame = frame.checked; changed(); });
    form.appendChild(field('Com moldura', frame));
    const color = el('input');
    color.type = 'color';
    color.value = widget.options?.color || '#f0abfc';
    color.addEventListener('change', () => { widget.options = { ...(widget.options || {}), color: color.value }; changed(); });
    form.appendChild(field('Cor de destaque', color));
    const size = el('input');
    size.type = 'range';
    size.min = '50';
    size.max = '300';
    size.step = '10';
    size.value = String(widget.options?.size || 100);
    size.addEventListener('change', () => { widget.options = { ...(widget.options || {}), size: Number(size.value) }; changed(); });
    form.appendChild(field('Tamanho do texto', size));
    (definition.optionDefs || []).forEach((def) => {
      let input;
      if (def.type === 'bool') { input = el('input'); input.type = 'checkbox'; input.checked = widget.options?.[def.key] ?? Boolean(def.default); }
      else if (def.type === 'textarea') { input = el('textarea', 'w-input'); input.rows = 3; input.value = widget.options?.[def.key] ?? ''; }
      else { input = el('input', 'w-input'); input.type = def.type === 'number' ? 'number' : def.type === 'datetime-local' ? 'datetime-local' : 'text'; input.value = widget.options?.[def.key] ?? def.default ?? ''; }
      input.addEventListener('change', () => {
        const value = def.type === 'bool' ? input.checked : def.type === 'number' ? Number(input.value) : input.value;
        widget.options = { ...(widget.options || {}), [def.key]: value };
        changed();
      });
      form.appendChild(field(def.label, input));
    });
    const remove = el('button', 'w-button sat-remove', 'Remover widget');
    remove.type = 'button';
    remove.addEventListener('click', () => { unmount(widget.id); widgets = widgets.filter((w) => w.id !== widget.id); save(); syncEmpty(); });
    form.appendChild(remove);
    instance.node.appendChild(form);
  }

  function mountWidget(widget) {
    const definition = widgetFor(widget.kind);
    if (!definition) return;
    const node = el('section', 'sat-widget');
    node.dataset.kind = widget.kind;
    const head = el('header', 'sat-widget-head');
    const title = el('span', 'sat-widget-title', definition.title);
    const more = el('button', 'sat-more', '⋯');
    more.type = 'button';
    more.title = 'Estilo, moldura, opções e remover';
    more.setAttribute('aria-label', `Opções de ${definition.title}`);
    head.appendChild(title);
    const body = el('div', 'sat-body-content');
    node.append(head, body, more);
    ['se', 'e', 's'].forEach((dir) => {
      const handle = el('span', `sat-resize sat-resize-${dir}`);
      handle.setAttribute('aria-hidden', 'true');
      handle.addEventListener('pointerdown', (event) => { event.stopPropagation(); track(event, widget, node, dir); });
      node.appendChild(handle);
    });
    stage.appendChild(node);
    const instance = { node, body, stop: null };
    instances.set(widget.id, instance);
    applyRect(widget, node);
    applyFrame(widget, definition, node);
    head.addEventListener('pointerdown', (event) => { if (!event.target.closest('button')) track(event, widget, node, 'drag'); });
    node.addEventListener('pointerdown', (event) => {
      if (!node.classList.contains('is-bare') || event.target.closest(INTERACTIVE) || event.target.closest('.sat-resize')) return;
      track(event, widget, node, 'drag-bare');
    });
    more.addEventListener('click', () => optionsMenu(widget, definition, instance));
    mountBody(widget, definition, instance);
  }

  function addWidget(kind, style = '') {
    const definition = widgetFor(kind);
    if (!definition || widgets.length >= 40) return;
    const fit = clampToViewport({ x: 0, y: 0, width: definition.size.w, height: definition.size.h }, stageSize(), { margin: 16, minWidth: MIN_W, minHeight: MIN_H });
    const size = { w: fit.width, h: fit.height };
    const spot = freeSpot(size);
    const widget = {
      id: `${kind}-${Math.random().toString(36).slice(2, 8)}`, kind, x: spot.x, y: spot.y, w: size.w, h: size.h,
      style: style || definition.styles?.[0]?.[0] || '', frame: !definition.bare, options: {},
    };
    widgets.push(widget);
    mountWidget(widget);
    save();
    syncEmpty();
  }

  function render(next) {
    [...instances.keys()].forEach(unmount);
    widgets = (next.widgets || []).map((w) => ({ x: 16, y: 16, w: 300, h: 200, style: '', options: {}, ...w }));
    locked = Boolean(next.locked);
    widgets.forEach(mountWidget);
    syncLock();
    syncEmpty();
  }

  // ---------- chrome: quebra-cabeça, galeria com prévias e travar ----------
  const puzzle = iconButton(PUZZLE, 'Widgets', 'sat-puzzle');
  const lockButton = iconButton(LOCK, 'Travar layout', 'sat-lock');
  const gallery = el('aside', 'sat-gallery');
  gallery.hidden = true;
  const bigPreview = el('div', 'sat-preview-large');
  const search = el('input', 'w-input');
  search.type = 'search';
  search.placeholder = 'Buscar widget ou estilo…';
  search.setAttribute('aria-label', 'Buscar widget');
  const chips = el('div', 'sat-chips');
  const cards = el('div', 'sat-cards');
  gallery.append(bigPreview, search, chips, cards);
  bigPreview.appendChild(el('p', 'w-muted', 'Passe o mouse num widget para ver a prévia'));
  let category = '';
  let previews = [];
  let largeStop = null;

  /** Renderiza o widget de verdade, em miniatura, dentro de um quadro (sem salvar nada). */
  function mountPreview(host, definition, style, boxSize) {
    clear(host);
    const inner = el('div', `sat-preview-inner${definition.bare ? ' is-bare' : ''}`);
    const body = el('div', 'sat-body-content');
    inner.appendChild(body);
    inner.style.width = `${definition.size.w}px`;
    inner.style.height = `${definition.size.h}px`;
    inner.style.transform = `scale(${Math.min(boxSize.w / definition.size.w, boxSize.h / definition.size.h, 1)})`;
    host.appendChild(inner);
    return runWidget(definition, { options: {}, style }, body, { preview: true });
  }

  function stopPreviews() {
    previews.forEach((stop) => stop());
    previews = [];
    largeStop?.();
    largeStop = null;
    clear(bigPreview);
    bigPreview.appendChild(el('p', 'w-muted', 'Passe o mouse num widget para ver a prévia'));
  }

  function showLarge(definition, style) {
    largeStop?.();
    largeStop = mountPreview(bigPreview, definition, style, { w: 320, h: 190 });
  }

  function drawGallery() {
    stopPreviews();
    clear(cards);
    clear(chips);
    ['', ...CATEGORIES].forEach((name) => {
      const chip = el('button', `sat-chip${name === category ? ' is-active' : ''}`, name || 'Todos');
      chip.type = 'button';
      chip.addEventListener('click', () => { category = name; drawGallery(); });
      chips.appendChild(chip);
    });
    catalog().filter((w) => matches(w, search.value, category)).forEach((definition) => {
      let style = definition.styles?.[0]?.[0] || '';
      const card = el('div', 'sat-card-button');
      const host = el('div', 'sat-preview');
      const add = el('button', 'sat-card-add');
      add.type = 'button';
      add.append(el('b', '', definition.title), el('span', '', `${definition.category}${variantCount(definition) > 1 ? ` · ${variantCount(definition)} estilos` : ''}`));
      card.append(host, add);
      let stop = mountPreview(host, definition, style, { w: 150, h: 92 });
      previews.push(() => stop());
      if (definition.styles) {
        const variants = el('div', 'sat-variants');
        definition.styles.forEach(([value, label]) => {
          const button = el('button', `sat-variant${value === style ? ' is-active' : ''}`, label);
          button.type = 'button';
          button.addEventListener('click', () => {
            style = value;
            variants.querySelectorAll('.sat-variant').forEach((b) => b.classList.toggle('is-active', b === button));
            stop();
            stop = mountPreview(host, definition, style, { w: 150, h: 92 });
            showLarge(definition, style);
          });
          variants.appendChild(button);
        });
        card.appendChild(variants);
      }
      card.addEventListener('mouseenter', () => showLarge(definition, style));
      add.addEventListener('click', () => addWidget(definition.kind, style));
      host.addEventListener('click', () => addWidget(definition.kind, style));
      cards.appendChild(card);
    });
  }
  search.addEventListener('input', drawGallery);
  puzzle.addEventListener('click', () => {
    gallery.hidden = !gallery.hidden;
    if (gallery.hidden) stopPreviews();
    else { drawGallery(); search.focus(); }
  });
  lockButton.addEventListener('click', () => { locked = !locked; syncLock(); save(); });

  const syncLock = () => {
    document.body.classList.toggle('is-locked', locked);
    lockButton.setAttribute('aria-pressed', String(locked));
    lockButton.title = locked ? 'Destravar layout' : 'Travar layout';
    if (locked) { gallery.hidden = true; stopPreviews(); }
  };
  const syncEmpty = () => document.body.classList.toggle('is-empty', widgets.length === 0);
  document.body.append(lockButton, puzzle, gallery);
  window.addEventListener('resize', () => widgets.forEach((widget) => {
    const instance = instances.get(widget.id);
    if (instance) applyRect(widget, instance.node);
  }));

  // ---------- conteúdo da Kira (imagens, criação, notícias...) ----------
  // Com esta janela à mostra, vem para cá o que o Chefe pede ("mostra na janela 2", "move esse painel pra cá") ou o
  // tipo de painel que ele já passou para cá. Mesmos painéis e o mesmo palco da principal (o anterior encolhe para o
  // lado). Minimizada ou coberta, o conteúdo vai para a principal.
  const contentLayer = el('div', 'kui-window-layer sat-kira-layer');
  document.body.appendChild(contentLayer);
  const contentBus = createEventBus();
  const contentDispatcher = createDispatcher({ windowManager: new WindowManager(contentLayer, { registry: createPanelRegistry(), bus: contentBus }), bus: contentBus });
  const reportPanel = (state) => (panel) => {
    if (!panel?.id) return;
    fetch('/api/ui/media/panels/state', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: panel.id, state }) }).catch(() => {});
  };
  [['window:close', 'closed'], ['window:touch', 'touched'], ['window:pin', 'pinned'], ['window:unpin', 'unpinned']].forEach(([event, state]) => contentBus.on(event, reportPanel(state)));
  // painel solto: se sob o ponto houver outra janela da Kira (ao lado ou por cima), o backend passa o painel para ela
  contentBus.on('window:drop', (drop) => {
    fetch('/api/ui/windows/transfer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ panel_id: drop.id, screen_x: drop.screenX, screen_y: drop.screenY }) }).catch(() => {});
  });
  // Com o widget "imagens" no layout, as imagens continuam nele (como antes); o resto vai para os painéis.
  const viewerWidgetIds = new Set();
  const forViewerWidget = (item) => widgets.some((widget) => widget.kind === 'viewer')
    && ((item.type === 'open_panel' && item.kind === 'viewer') || (['update_panel', 'close_panel'].includes(item.type) && viewerWidgetIds.has(item.id)));

  // ---------- sockets (somente leitura; sem LiveKit, sem sessão, sem áudio) ----------
  let retry = 1000;
  function connect(path, onMessage) {
    const open = () => {
      const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`);
      socket.onopen = () => { retry = 1000; };
      socket.onmessage = (event) => { try { onMessage(JSON.parse(event.data)); } catch (_) { /* mensagem inválida */ } };
      socket.onclose = () => setTimeout(open, (retry = Math.min(retry * 2, 15000)));
    };
    open();
  }

  connect('/ws/transcription', (data) => {
    if (data?.type !== 'transcription' && data?.type !== 'transcript_message') return;
    const text = String(data.text || '').trim();
    if (!text || (data.is_final ?? data.final ?? true) === false) return;
    const raw = String(data.speaker || data.role || data.source || '').toLowerCase();
    const line = { role: /user|usuario|voce|você|you|mic|voice/.test(raw) ? 'user' : 'kira', text };
    transcriptBuffer.push(line);
    if (transcriptBuffer.length > 80) transcriptBuffer.shift();
    bus.emit('transcript', line);
  });
  connect('/ws/runtime-status', (snapshot) => { lastRuntime = snapshot; bus.emit('runtime', snapshot); });
  const loadRecord = () => fetch('/api/ui/windows', { cache: 'no-store' })
    .then((response) => (response.ok ? response.json() : null))
    .then((payload) => { const mine = payload?.windows?.find((item) => item.windowId === windowId); if (mine) render(mine); })
    .catch(() => {});
  connect(`/ws/ui-actions?window=${windowId}`, (raw) => {
    const mine = filterForWindow(raw, windowId);
    if (!mine) return;
    (Array.isArray(mine) ? mine : [mine]).forEach((item) => {
      if (item.type === 'window') { if (item.reload) loadRecord(); return; }
      if (forViewerWidget(item)) {
        if (item.type === 'open_panel') viewerWidgetIds.add(item.id);
        bus.emit('action', item);
        return;
      }
      contentDispatcher.dispatch(item);
    });
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') instances.forEach((i) => i.node.querySelector('.sat-options')?.remove()); });
  loadRecord();
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootstrap, { once: true });
  else bootstrap();
}
