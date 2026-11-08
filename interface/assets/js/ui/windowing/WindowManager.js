import { MAX_MINIS, MEDIA_PANEL_SPECS, PANEL_DEFAULT_SIZES, clampToViewport, fitSize, freeStageArea, placeMediaPanels, placeSingle, stagePanels } from './placement.js';
import { renderTitleIcon, titleIconName } from './titleIcons.js';

const MIN_W = 260;
const MIN_H = 160;
const MIN_TITLE_VISIBLE = 80;
const TITLE_VISIBLE_HEIGHT = 36;
const Z_BASE = 340;
const Z_PINNED = 7900;
const Z_MAX = 8000;
const CLOSE_ANIMATION_MS = 240;
/** O ponteiro saiu da área desta janela (arrastando um painel para outra janela da Kira). */
function isOutside(win, event) {
  const x = Number(event?.clientX);
  const y = Number(event?.clientY);
  const width = Number(win?.innerWidth);
  const height = Number(win?.innerHeight);
  if (!Number.isFinite(x) || !Number.isFinite(y) || !(width > 0) || !(height > 0)) return false;
  return x < 0 || y < 0 || x > width || y > height;
}

/** Ponto da tela (pixels físicos) do mouse: posição da janela + borda/título + posição dentro da página. Não usa o
 *  screenX do evento, que nos eventos sintéticos do QtWebEngine vem igual ao clientX. */
function screenPoint(win, event) {
  const ratio = Number(win?.devicePixelRatio) || 1;
  const frameX = Math.max(0, (number(win?.outerWidth) - number(win?.innerWidth)) / 2);
  const frameY = Math.max(0, number(win?.outerHeight) - number(win?.innerHeight) - frameX);
  return {
    screenX: Math.round((number(win?.screenX) + frameX + number(event?.clientX)) * ratio),
    screenY: Math.round((number(win?.screenY) + frameY + number(event?.clientY)) * ratio),
  };
}

/** Velocidade das animações (aba Aparência): multiplica as durações; o fechar espera a animação inteira. */
function animSpeed(win) {
  try {
    const value = Number.parseFloat(win?.getComputedStyle?.(win.document?.documentElement)?.getPropertyValue?.('--kira-anim-speed'));
    return Number.isFinite(value) && value >= 0 ? value : 1;
  } catch (_) {
    return 1;
  }
}
// Troca de destaque: o painel anterior encolhe e desliza para o lado neste tempo (só left/top/transform).
const STAGE_MOVE_MS = 520;
const SNAP_THRESHOLD = 18;
const LAYOUT_STORAGE_KEY = 'kira.ui.layout.v1';
const DRAG_THRESHOLD = 4;
const LAYOUT_VERSION = 1;
const RESIZE_HANDLES = Object.freeze(['n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw']);
const VIEW_IDS = Object.freeze(['main', 'chat']);
const MEDIA_KINDS = Object.freeze(Object.keys(MEDIA_PANEL_SPECS));

function number(value, fallback = 0) {
  const result = Number(value);
  return Number.isFinite(result) ? result : fallback;
}
function positive(value, fallback) { const result = number(value, fallback); return result > 0 ? result : fallback; }
function clamp(value, min, max) { return Math.min(Math.max(value, min), Math.max(min, max)); }
function safeText(value, fallback = '') { const text = String(value == null ? '' : value).trim(); return text || fallback; }
function safeId(value) { return safeText(value).replace(/[^a-z0-9_-]+/gi, '-').replace(/^-+|-+$/g, '').slice(0, 64); }
function reducedMotion(windowRef) {
  const mode = windowRef?.document?.documentElement?.dataset?.kiraMotion || 'full';
  if (mode === 'off') return true;
  if (mode !== 'system') return false;
  try { return Boolean(windowRef?.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches); } catch (_) { return false; }
}
function isInputTarget(target) { return Boolean(target?.matches?.('input, textarea, select, [contenteditable="true"]')); }
function hasDirection(handle, direction) { return String(handle).includes(direction); }

function rectFrom(value) {
  return { x: number(value?.x), y: number(value?.y), width: positive(value?.width, MIN_W), height: positive(value?.height, MIN_H) };
}

/** Pure resize calculation used by the pointer handler and Node tests. */
export function resizeRect(initial, deltaX, deltaY, handle, bounds, minWidth = MIN_W, minHeight = MIN_H) {
  const start = rectFrom(initial);
  const area = { width: positive(bounds?.width, start.width), height: positive(bounds?.height, start.height) };
  let left = start.x;
  let top = start.y;
  let right = start.x + start.width;
  let bottom = start.y + start.height;

  if (hasDirection(handle, 'w')) left = start.x + number(deltaX);
  if (hasDirection(handle, 'e')) right = start.x + start.width + number(deltaX);
  if (hasDirection(handle, 'n')) top = start.y + number(deltaY);
  if (hasDirection(handle, 's')) bottom = start.y + start.height + number(deltaY);

  if (right - left < minWidth) {
    if (hasDirection(handle, 'w')) left = right - minWidth;
    else right = left + minWidth;
  }
  if (bottom - top < minHeight) {
    if (hasDirection(handle, 'n')) top = bottom - minHeight;
    else bottom = top + minHeight;
  }

  if (right - left > area.width) {
    if (hasDirection(handle, 'w')) left = right - area.width;
    else right = left + area.width;
  }
  if (bottom - top > area.height) {
    if (hasDirection(handle, 'n')) top = bottom - area.height;
    else bottom = top + area.height;
  }

  if (left < 0) { if (hasDirection(handle, 'w')) left = 0; else right -= left; left = 0; }
  if (top < 0) { if (hasDirection(handle, 'n')) top = 0; else bottom -= top; top = 0; }
  if (right > area.width) { if (hasDirection(handle, 'e')) right = area.width; else left -= right - area.width; right = area.width; }
  if (bottom > area.height) { if (hasDirection(handle, 's')) bottom = area.height; else top -= bottom - area.height; bottom = area.height; }

  return {
    x: Math.round(clamp(left, 0, Math.max(0, area.width - minWidth))),
    y: Math.round(clamp(top, 0, Math.max(0, area.height - minHeight))),
    width: Math.round(Math.max(minWidth, right - left)),
    height: Math.round(Math.max(minHeight, bottom - top)),
  };
}

export function snapTarget(x, y, bounds, threshold = SNAP_THRESHOLD) {
  const width = positive(bounds?.width, 0);
  const height = positive(bounds?.height, 0);
  if (number(y) <= threshold) return 'max';
  if (number(x) <= threshold) return 'left';
  if (number(x) >= width - threshold) return 'right';
  return null;
}

export class WindowManager {
  constructor(root, { documentRef = globalThis.document, windowRef = globalThis.window, registry, bus = null, store = null, logger = globalThis.console, resizeObserver = globalThis.ResizeObserver } = {}) {
    if (!root || typeof root.appendChild !== 'function') throw new TypeError('WindowManager exige #kira-ui-root.');
    if (!registry || typeof registry.get !== 'function') throw new TypeError('WindowManager exige um panelRegistry.');
    this.root = root;
    this.document = documentRef;
    this.window = windowRef;
    this.registry = registry;
    this.bus = bus;
    this.store = store;
    this.logger = logger;
    this.ResizeObserver = resizeObserver;
    this.records = new Map();
    this.sequence = 0;
    this.zIndex = Z_BASE;
    this.pinnedSequence = 0;
    this.activeView = 'main';
    this.savedLayout = this._readLayout();
    this.destroyed = false;
    this._resizeObserver = null;
    this._rootListeners = [];
    this._createLayerChrome();
    this._bindRoot();
    this._observeRoot();
    this.root.hidden = false;
  }

  open(kind, options = {}) {
    if (this.destroyed) return null;
    const panelKind = safeText(kind).toLowerCase();
    if (!this.registry.has(panelKind)) { this._warn(`painel não registrado: ${panelKind}`); return null; }
    // Painel novo com WhatsApp/Internet/Música/Configurações aberta: a aba sai do meio para o painel aparecer (2026-10-31).
    try { if (this.window?.document?.body?.classList?.contains('drawer-open')) this.window.closeDrawers?.(); } catch (_) { /* sem abas */ }
    const requestedId = safeId(options.id);
    const id = requestedId || `kui-${panelKind}-${++this.sequence}`;
    const existing = this.records.get(id);
    if (existing && existing.state !== 'closed') {
      this.update(id, options.props || {});
      if (options.title) this._setTitle(existing, options.title);
      if (VIEW_IDS.includes(options.view)) existing.view = options.view;
      if (options.focus !== false) this.focus(id);
      this._render(existing);
      return this.get(id);
    }
    if (existing) this._cancelClose(existing);

    const bounds = this._bounds();
    const mediaSpec = MEDIA_PANEL_SPECS[panelKind] || PANEL_DEFAULT_SIZES[panelKind];
    const fitted = mediaSpec ? fitSize(mediaSpec, bounds) : null;
    const width = clamp(number(options.width, fitted?.width || Math.min(480, bounds.width - 32)), mediaSpec?.minWidth || MIN_W, Math.max(mediaSpec?.minWidth || MIN_W, bounds.width));
    const height = clamp(number(options.height, fitted?.height || Math.min(320, bounds.height - 32)), mediaSpec?.minHeight || MIN_H, Math.max(mediaSpec?.minHeight || MIN_H, bounds.height));
    const record = existing || {
      id, kind: panelKind, createdAt: ++this.sequence, element: null, content: null, titleElement: null,
      titlebar: null, state: 'closed', pinned: false, userTouched: false, cleanup: null, timer: null,
      restoreRect: null, props: {}, x: 0, y: 0, width, height, zIndex: Z_BASE,
      view: 'main',
    };
    record.kind = panelKind;
    record.title = safeText(options.title, panelKind);
    record.props = options.props && typeof options.props === 'object' ? { ...options.props } : {};
    record.createdBy = options.createdBy === 'kira' ? 'kira' : 'user';
    record.view = VIEW_IDS.includes(options.view) ? options.view : (record.view || this.activeView);
    record.autoCloseMs = number(options.autoCloseMs, 0);
    record.x = number(options.x, record.x || 24);
    record.y = number(options.y, record.y || 24);
    record.width = options.width != null || !existing ? width : record.width;
    record.height = options.height != null || !existing ? height : record.height;
    record.state = 'open';
    record.visible = true;
    if (record.createdBy === 'kira') record.featuredAt = ++this.sequence;
    record.element ||= this._createElement(record);
    this._refreshIcon(record);
    if (!existing) {
      this.records.set(id, record);
      this.root.appendChild(record.element);
      this._mountContent(record);
    }
    const persisted = !existing && !this._isMediaKind(panelKind) && options.x == null && options.y == null ? this.savedLayout.panels?.[panelKind] : null;
    if (persisted) {
      record.x = number(persisted.x, record.x);
      record.y = number(persisted.y, record.y);
      record.width = positive(persisted.width, record.width);
      record.height = positive(persisted.height, record.height);
      this._clampRecord(record, bounds);
    } else {
      this._placeNew(record, bounds, options);
    }
    if (this._phoneMode()) {
      if (!existing) record.featuredAt = ++this.sequence;
      this._placePhone();
      if (!existing && record.createdBy === 'kira') this._spawnFromOrb(record, bounds);
      this._armAutoClose(record);
      if (options.focus !== false) this.focus(id);
      return this.get(id);
    }
    this._render(record);
    if (!existing && record.createdBy === 'kira') this._spawnFromOrb(record, bounds);
    if (this._isMediaKind(panelKind)) this.placeMediaPanels();
    if (!existing && !persisted && options.x == null && options.y == null) this._settleAfterLayout(record);
    this._armAutoClose(record);
    if (options.focus !== false) this.focus(id);
    return this.get(id);
  }

  close(id) {
    const record = this.records.get(String(id));
    if (!record || record.state === 'closed') return false;
    if (record.gesture) this._finishGesture(record);
    this._cancelAutoClose(record);
    record.state = 'closed';
    record.visible = false;
    record.element?.classList?.add('is-closing');
    this._removeDockItem(record);
    this._render(record);
    record.timer = this._setTimeout(() => this._finalizeClose(record), reducedMotion(this.window) ? 0 : CLOSE_ANIMATION_MS * animSpeed(this.window));
    this.bus?.emit?.('window:close', this.get(record.id));
    // a miniatura mais recente volta ao destaque
    if (this._phoneMode()) this._placePhone();
    else if (this._isMediaKind(record.kind)) { if (record.staged) this.placeMediaPanels(); else this._renderMediaTabs(); }
    return true;
  }

  /** Traz um painel da Kira de volta ao destaque; o que estava em destaque vira miniatura ao lado. */
  feature(id) {
    const record = this.records.get(String(id));
    if (!record || record.state !== 'open' || !(this._isMediaKind(record.kind) || this._phoneMode())) return false;
    record.featuredAt = ++this.sequence;
    this.placeMediaPanels();
    this.focus(record.id);
    return true;
  }

  focus(id) {
    const record = this.records.get(String(id));
    if (!record || record.state === 'closed' || record.state === 'minimized' || record.view !== this.activeView) return false;
    if (record.pinned) record.zIndex = Z_PINNED + (++this.pinnedSequence % (Z_MAX - Z_PINNED + 1));
    else { this.zIndex = this.zIndex >= 7800 ? Z_BASE : this.zIndex + 1; record.zIndex = this.zIndex; }
    this._render(record);
    try { record.element?.focus?.({ preventScroll: true }); } catch (_) { try { record.element?.focus?.(); } catch (_) {} }
    return true;
  }

  minimize(id) {
    const record = this.records.get(String(id));
    if (!record || record.state === 'closed') return false;
    if (record.state === 'maximized') record.restoreRect ||= this._rect(record);
    record.state = 'minimized';
    record.visible = false;
    this._render(record);
    this._renderDockItem(record);
    return true;
  }

  restore(id) {
    const record = this.records.get(String(id));
    if (!record) return false;
    if (record.state === 'maximized' && record.restoreRect) Object.assign(record, record.restoreRect);
    record.restoreRect = null;
    record.state = 'open';
    record.visible = true;
    this._renderDockItem(record);
    this._render(record);
    this.focus(id);
    return true;
  }

  toggleMax(id) {
    const record = this.records.get(String(id));
    if (!record || record.state === 'closed') return false;
    if (record.state === 'maximized') return this.restore(id);
    record.restoreRect = this._rect(record);
    const bounds = this._bounds();
    Object.assign(record, { x: 0, y: 0, width: bounds.width, height: bounds.height, state: 'maximized', visible: true });
    this._renderDockItem(record);
    this._render(record);
    this.focus(id);
    return true;
  }

  _touch(record) {
    if (record.miniScale) { record.miniScale = 0; record.staged = false; this._render(record); }
    if (record.userTouched) return;
    record.userTouched = true;
    this.bus?.emit?.('window:touch', this.get(record.id));
  }

  togglePin(id) {
    const record = this.records.get(String(id));
    if (!record || record.state === 'closed') return false;
    record.pinned = !record.pinned;
    this._touch(record);
    this.bus?.emit?.(record.pinned ? 'window:pin' : 'window:unpin', this.get(record.id));
    this.focus(id);
    this._render(record);
    return record.pinned;
  }

  update(id, props = {}) {
    const record = this.records.get(String(id));
    if (!record || !props || typeof props !== 'object' || Array.isArray(props)) return false;
    record.props = { ...record.props, ...props };
    this._refreshIcon(record);
    const definition = this.registry.get(record.kind);
    try { definition?.update?.(record.content, record.props, this._context(record)); } catch (error) { this._warn('update do painel falhou.', error); }
    return this.get(id);
  }

  get(id) { const record = this.records.get(String(id)); return record ? this._snapshot(record) : null; }
  countByCreator(creator) { return [...this.records.values()].filter((record) => record.state !== 'closed' && record.createdBy === creator).length; }
  oldestByCreator(creator, predicate = null) {
    const list = [...this.records.values()].filter((record) => record.state !== 'closed' && record.createdBy === creator && (!predicate || predicate(this._snapshot(record))));
    list.sort((left, right) => left.createdAt - right.createdAt);
    return list[0] ? this._snapshot(list[0]) : null;
  }
  serialize() { return [...this.records.values()].filter((record) => record.state !== 'closed').map((record) => this._snapshot(record)); }

  saveLayout() {
    const panels = { ...this.savedLayout.panels };
    this.records.forEach((record) => {
      if (record.state === 'closed' || record.state === 'minimized' || record.state === 'maximized') return;
      if (this._isMediaKind(record.kind)) return;
      panels[record.kind] = {
        x: Math.round(record.x), y: Math.round(record.y),
        width: Math.round(record.width), height: Math.round(record.height),
      };
    });
    const payload = { version: LAYOUT_VERSION, viewport: { width: Math.round(this._bounds().width), height: Math.round(this._bounds().height) }, panels };
    try { this.window?.localStorage?.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(payload)); } catch (error) { this._warn('não foi possível salvar o layout.', error); }
    this.savedLayout = payload;
    return payload;
  }

  resetLayout() {
    try { this.window?.localStorage?.removeItem(LAYOUT_STORAGE_KEY); } catch (error) { this._warn('não foi possível limpar o layout.', error); }
    this.savedLayout = { version: LAYOUT_VERSION, viewport: {}, panels: {} };
    const bounds = this._bounds();
    this.records.forEach((record) => {
      if (record.state === 'closed' || record.state === 'minimized' || record.state === 'maximized') return;
      this._placeNew(record, bounds, {});
      record.userTouched = false;
      this._render(record);
    });
    this.placeMediaPanels();
    return true;
  }

  applyLayout(preset) {
    const normalized = safeText(preset).toLowerCase();
    if (normalized === 'default') return this.resetLayout();
    if (normalized === 'focus') {
      const bounds = this._bounds();
      this.records.forEach((record) => {
        if (record.state !== 'closed') Object.assign(record, { x: 0, y: 0, width: bounds.width, height: bounds.height, state: 'maximized', visible: true });
      });
      this.records.forEach((record) => this._render(record));
      return true;
    }
    return false;
  }

  showView(viewId) {
    const nextView = safeText(viewId).toLowerCase();
    if (!VIEW_IDS.includes(nextView)) return false;
    this.activeView = nextView;
    this.records.forEach((record) => this._render(record));
    return true;
  }

  hideView(viewId) {
    const targetView = safeText(viewId).toLowerCase();
    if (!VIEW_IDS.includes(targetView)) return false;
    if (this.activeView === targetView) this.activeView = 'main';
    this.records.forEach((record) => this._render(record));
    return true;
  }

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.records.forEach((record) => {
      this._cancelAutoClose(record);
      this._finishGesture(record);
      try { record.cleanup?.(); } catch (_) {}
      record.element?.remove?.();
    });
    this.records.clear();
    this._rootListeners.forEach(({ target, type, handler }) => target.removeEventListener?.(type, handler));
    this._rootListeners = [];
    this._resizeObserver?.disconnect?.();
    this._resizeObserver = null;
    this._dock?.remove?.();
    this._mediaTabs?.remove?.();
    this._mediaTabs = null;
    this._snapPreview?.remove?.();
    this.root.hidden = true;
  }

  _createLayerChrome() {
    this._mediaTabs = this.document.createElement('nav');
    this._mediaTabs.className = 'kui-media-tabs';
    this._mediaTabs.setAttribute('aria-label', 'Contêineres de mídia');
    this._mediaTabs.hidden = true;
    this.root.appendChild(this._mediaTabs);
    this._dock = this.document.createElement('div');
    this._dock.className = 'kui-dock';
    this._dock.setAttribute('aria-label', 'Painéis minimizados da Kira');
    this._dock.hidden = true;
    this.root.appendChild(this._dock);
    this._snapPreview = this.document.createElement('div');
    this._snapPreview.className = 'kui-snap-preview';
    this._snapPreview.hidden = true;
    this.root.appendChild(this._snapPreview);
  }

  _bindRoot() {
    const onPointerDown = (event) => {
      const element = event.target?.closest?.('.kui-window');
      const id = element?.dataset?.kuiWindowId;
      if (id) this.focus(id);
    };
    const onClick = (event) => {
      const mini = this.records.get(event.target?.closest?.('.kui-window')?.dataset?.kuiWindowId || '');
      if (mini?.miniScale && !event.target?.closest?.('[data-kui-action]')) { this.feature(mini.id); return; }
      const button = event.target?.closest?.('[data-kui-action]');
      if (!button || !this.root.contains?.(button)) return;
      event.preventDefault?.();
      const id = button.closest?.('.kui-window')?.dataset?.kuiWindowId || button.dataset.kuiWindowId;
      if (!id) return;
      const action = button.dataset.kuiAction;
      if (action === 'minimize') this.minimize(id);
      if (action === 'maximize') this.toggleMax(id);
      if (action === 'pin') this.togglePin(id);
      if (action === 'close') this.close(id);
      if (action === 'restore') this.restore(id);
    };
    const onKeyDown = (event) => {
      if (event.key !== 'Escape' || isInputTarget(event.target) || isInputTarget(this.document.activeElement)) return;
      const element = event.target?.closest?.('.kui-window') || this.document.activeElement?.closest?.('.kui-window');
      if (element?.dataset?.kuiWindowId) { event.preventDefault?.(); this.close(element.dataset.kuiWindowId); }
    };
    const onDoubleClick = (event) => {
      const titlebar = event.target?.closest?.('.kui-titlebar');
      const id = titlebar?.closest?.('.kui-window')?.dataset?.kuiWindowId;
      if (id && !event.target?.closest?.('button')) this.toggleMax(id);
    };
    [[this.root, 'pointerdown', onPointerDown], [this.root, 'click', onClick], [this.root, 'keydown', onKeyDown], [this.root, 'dblclick', onDoubleClick]].forEach(([target, type, handler]) => {
      target.addEventListener?.(type, handler);
      this._rootListeners.push({ target, type, handler });
    });
  }

  _observeRoot() {
    if (typeof this.ResizeObserver === 'function') {
      this._resizeObserver = new this.ResizeObserver(() => this._onResize());
      this._resizeObserver.observe?.(this.root);
    } else {
      const handler = () => this._onResize();
      this.window?.addEventListener?.('resize', handler);
      this._rootListeners.push({ target: this.window, type: 'resize', handler });
    }
    // celular: a faixa de música abriu ou fechou; os painéis se rearrumam acima dela
    const phoneLayout = () => { if (this._phoneMode()) this._placePhone(); };
    this.window?.addEventListener?.('kira:phone-layout', phoneLayout);
    this._rootListeners.push({ target: this.window, type: 'kira:phone-layout', handler: phoneLayout });
  }

  _onResize() {
    const bounds = this._bounds();
    this.records.forEach((record) => {
      if (record.state === 'closed' || record.state === 'minimized') return;
      if (record.state === 'maximized') Object.assign(record, { x: 0, y: 0, width: bounds.width, height: bounds.height });
      else if (!record.userTouched && !record.pinned) Object.assign(record, clampToViewport(record, bounds, { minWidth: MIN_W, minHeight: MIN_H }));
      else this._clampRecord(record, bounds);
      this._render(record);
    });
    this.placeMediaPanels();
  }

  _createElement(record) {
    const element = this.document.createElement('article');
    const titlebar = this.document.createElement('header');
    const icon = this.document.createElement('span');
    const title = this.document.createElement('span');
    const controls = this.document.createElement('div');
    const content = this.document.createElement('section');
    element.className = 'kui-window';
    element.id = `kui-window-${safeId(record.id)}`;
    element.tabIndex = -1;
    element.setAttribute('role', 'dialog');
    element.setAttribute('aria-modal', 'false');
    titlebar.className = 'kui-titlebar';
    titlebar.dataset.kuiDragHandle = 'true';
    icon.className = 'kui-title-icon';
    record.iconElement = icon;
    this._refreshIcon(record);
    title.className = 'kui-title';
    title.textContent = record.title;
    controls.className = 'kui-window-controls';
    [['minimize', '−', 'Minimizar'], ['maximize', '□', 'Maximizar'], ['pin', '⌖', 'Fixar'], ['close', '×', 'Fechar']].forEach(([action, label, text]) => {
      const button = this.document.createElement('button');
      button.type = 'button';
      button.className = `kui-window-button kui-window-button-${action}`;
      button.dataset.kuiAction = action;
      button.setAttribute('aria-label', `${text} ${record.title}`);
      button.setAttribute('title', `${text} ${record.title}`);
      button.textContent = label;
      controls.appendChild(button);
    });
    content.className = 'kui-content';
    titlebar.appendChild(icon);
    titlebar.appendChild(title);
    titlebar.appendChild(controls);
    element.appendChild(titlebar);
    element.appendChild(content);
    RESIZE_HANDLES.forEach((direction) => {
      const handle = this.document.createElement('div');
      handle.className = `kui-resize-handle kui-resize-${direction}`;
      handle.dataset.kuiResize = direction;
      handle.setAttribute('aria-label', `Redimensionar ${record.title} ${direction}`);
      handle.setAttribute('role', 'presentation');
      element.appendChild(handle);
      handle.addEventListener?.('pointerdown', (event) => this._startResize(record, event, direction));
    });
    titlebar.addEventListener?.('pointerdown', (event) => this._startDrag(record, event));
    record.titlebar = titlebar;
    record.titleElement = title;
    record.content = content;
    return element;
  }

  _mountContent(record) {
    const definition = this.registry.get(record.kind);
    if (!definition) return;
    try { record.cleanup = definition.mount(record.content, this._context(record)) || null; } catch (error) { this._warn('mount do painel falhou.', error); record.content.textContent = 'Não foi possível carregar este painel.'; }
  }

  _context(record) { return { id: record.id, kind: record.kind, props: record.props, bus: this.bus, store: this.store, get: () => this.get(record.id), update: (props) => this.update(record.id, props), close: () => this.close(record.id), focus: () => this.focus(record.id) }; }
  _isMediaKind(kind) { return MEDIA_KINDS.includes(kind); }
  /** Painel aberto pela Kira nasce da orbe (centro da tela) e volta para ela ao fechar. */
  _spawnFromOrb(record, bounds) {
    const element = record.element;
    if (!element?.style?.setProperty) return;
    element.style.setProperty('--kui-spawn-x', `${Math.round(bounds.width / 2 - (record.x + record.width / 2))}px`);
    element.style.setProperty('--kui-spawn-y', `${Math.round(bounds.height / 2 - (record.y + record.height / 2))}px`);
    element.dataset.kuiSpawn = 'orb';
    // Sorteio (pedido do Chefe, 2026-11-05): feixe que se divide, onda de choque ou portal. Só transform/opacity/clip.
    const fx = ['feixe', 'onda', 'portal'][Math.floor(Math.random() * 3)];
    element.dataset.kuiFx = fx;
    if (fx === 'portal' || reducedMotion(this.window) || typeof this.document?.createElement !== 'function') return;
    const spark = this.document.createElement('div');
    spark.className = `kui-spawn-fx kui-spawn-${fx}`;
    spark.style.left = `${Math.round(bounds.width / 2)}px`;
    spark.style.top = `${Math.round(bounds.height / 2)}px`;
    this.root.appendChild(spark);
    const dx = record.x + record.width / 2 - bounds.width / 2; const dy = record.y + record.height / 2 - bounds.height / 2;
    const frames = fx === 'onda'
      ? [{ transform: 'scale(.3)', opacity: 0.9 }, { transform: 'scale(9)', opacity: 0 }]
      : [{ transform: 'translate(0,0) scale(.4)', opacity: 1 }, { transform: 'translate(0,-60px) scale(1)', opacity: 1, offset: 0.35 }, { transform: `translate(${dx}px,${dy}px) scale(.7)`, opacity: 0 }];
    const anim = spark.animate?.(frames, { duration: 520 * animSpeed(this.window), easing: 'cubic-bezier(.22,.9,.28,1)' });
    if (anim) anim.onfinish = () => spark.remove(); else spark.remove();
  }

  _refreshIcon(record) {
    const name = titleIconName(record.kind, record.props, record.iconElement?.dataset?.icon);
    if (record.iconElement && record.iconElement.dataset?.icon !== name) renderTitleIcon(record.iconElement, name, this.document);
  }

  _setTitle(record, title) { record.title = safeText(title, record.title).slice(0, 120); if (record.titleElement) record.titleElement.textContent = record.title; }
  _bounds() {
    const rect = this.root.getBoundingClientRect?.() || {};
    return { width: positive(rect.width, positive(this.root.clientWidth, positive(this.window?.innerWidth, 1100))), height: positive(rect.height, positive(this.root.clientHeight, positive(this.window?.innerHeight, 700))), left: number(rect.left), top: number(rect.top) };
  }
  _rect(record) { return { x: record.x, y: record.y, width: record.width, height: record.height }; }
  _clampRecord(record, bounds) {
    record.width = clamp(record.width, MIN_W, Math.max(MIN_W, bounds.width));
    record.height = clamp(record.height, MIN_H, Math.max(MIN_H, bounds.height));
    record.x = clamp(record.x, 0, Math.max(0, bounds.width - Math.min(MIN_TITLE_VISIBLE, record.width)));
    record.y = clamp(record.y, 0, Math.max(0, bounds.height - Math.min(TITLE_VISIBLE_HEIGHT, record.height)));
    if (record.x + record.width > bounds.width) record.x = Math.max(0, bounds.width - record.width);
    if (record.y + record.height > bounds.height) record.y = Math.max(0, bounds.height - record.height);
  }
  _forbiddenRects(bounds) {
    const selectors = [
      '#bottom-dock', '#decorative-bar', '.side-png-button', '#drawer-reminders-btn-float', '#drawer-config-btn-float',
      '#logo-container', '.header-left', '#header-clock', '.header-clock', '#kira-clock', '.kira-clock',
      '#kira-session-status-panel', '#kira-api-identity-panel', '.kira-api-identity-panel',
      '#kira-transcript-panel', '.left-transcription-card', '.left-panel-card', '.kui-hamburger', '.kui-sidebar', '.kui-sidebar-backdrop', '.kui-media-tabs',
    ];
    const rootRect = this.root.getBoundingClientRect?.() || { left: 0, top: 0 };
    const result = [];
    selectors.forEach((selector) => {
      try {
        this.document.querySelectorAll?.(selector)?.forEach((node) => {
          if (node.hidden || node.dataset?.kuiFloating === 'true') return; // transcrição flutuante do celular: fica por cima
          const style = this.window?.getComputedStyle?.(node) || globalThis.getComputedStyle?.(node);
          if (style && (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0')) return;
          const rect = node.getBoundingClientRect?.();
          if (rect?.width && rect?.height) result.push({ x: rect.left - rootRect.left, y: rect.top - rootRect.top, width: rect.width, height: rect.height });
        });
      } catch (_) { /* selector is optional */ }
    });
    return result.filter((rect) => rect.x < bounds.width && rect.y < bounds.height && rect.x + rect.width > 0 && rect.y + rect.height > 0);
  }
  /** Painéis que a Kira abriu e ninguém mexeu, do destaque (mais novo) para o mais antigo. */
  _stageRecords() {
    return [...this.records.values()]
      .filter((record) => record.state === 'open' && record.createdBy === 'kira' && this._isMediaKind(record.kind) && !record.userTouched && !record.pinned && record.view === this.activeView)
      .sort((left, right) => (right.featuredAt || right.createdAt) - (left.featuredAt || left.createdAt));
  }

  /**
   * Mais de um painel da Kira: o novo fica em destaque e os anteriores encolhem e deslizam para a coluna do lado,
   * como no briefing (antes o anterior era fechado ou os dois se espremiam lado a lado).
   */
  _placeStage(stage, bounds) {
    const [featured, ...older] = stage;
    const minis = older.slice(0, MAX_MINIS);
    const spec = MEDIA_PANEL_SPECS[featured.kind] || { width: featured.width, height: featured.height, minWidth: MIN_W, minHeight: MIN_H };
    const plan = stagePanels({
      area: freeStageArea(bounds, this._forbiddenRects(bounds)),
      featured: { id: featured.id, ...spec },
      minis: minis.map((record) => ({ id: record.id, width: record.width, height: record.height })),
    });
    const moving = [featured, ...minis];
    moving.forEach((record) => this._startStageMove(record));
    Object.assign(featured, { x: plan.featured.x, y: plan.featured.y, width: plan.featured.width, height: plan.featured.height, miniScale: 0, staged: true, autoPlaced: true });
    plan.minis.forEach((spot) => Object.assign(this.records.get(spot.id), { x: spot.x, y: spot.y, miniScale: spot.scale, staged: true, autoPlaced: true }));
    moving.forEach((record) => this._render(record));
    return [{ ...plan.featured, scale: 1 }, ...plan.minis.map((spot) => ({ ...spot, width: this.records.get(spot.id).width, height: this.records.get(spot.id).height }))];
  }

  _startStageMove(record) {
    if (reducedMotion(this.window) || !record.element) return;
    // FLIP (2026-10-31): o painel vai direto para o lugar novo e um translate/scale inverso anima até lá no compositor.
    // Antes animava left/top/width, que refaz a tela inteira a cada quadro (troca de painel grande/pequeno lagava).
    const element = record.element;
    if (typeof element.animate === 'function' && typeof element.getBoundingClientRect === 'function') {
      const before = element.getBoundingClientRect();
      const run = () => {
        const after = element.getBoundingClientRect();
        if (!before.width || !after.width) return;
        const dx = before.left - after.left;
        const dy = before.top - after.top;
        const sx = before.width / after.width;
        const sy = before.height / after.height;
        if (Math.abs(dx) < 1 && Math.abs(dy) < 1 && Math.abs(sx - 1) < 0.01) return;
        const base = element.style.transform || '';
        element.animate([
          { transformOrigin: 'top left', transform: `translate(${dx}px, ${dy}px) scale(${sx}, ${sy}) ${base}` },
          { transformOrigin: 'top left', transform: base || 'none' },
        ], { duration: STAGE_MOVE_MS * animSpeed(this.window), easing: 'cubic-bezier(.2, .85, .25, 1)' }); // segue o tempo das animações
      };
      (this.window?.requestAnimationFrame || ((fn) => setTimeout(fn, 16)))(run);
      return;
    }
    record.stageMoving = true;
    record.element.classList?.add?.('kui-stage-move');
    if (record.stageTimer) this._clearTimeout(record.stageTimer);
    record.stageTimer = this._setTimeout(() => {
      record.stageMoving = false;
      record.stageTimer = null;
      record.element?.classList?.remove?.('kui-stage-move');
      this._render(record);
    }, STAGE_MOVE_MS);
  }

  /** Celular (tela estreita na página aberta pelo portão): arranjo próprio, sem o do PC. */
  _phoneMode() { return Boolean(globalThis.KIRA_REMOTE) && number(this.window?.innerWidth, 1100) <= 760; }

  /**
   * Celular: o painel mais novo ocupa o meio da tela (entre o topo e o rodapé); os anteriores (até 3) encolhem para
   * uma fileira à direita, acima do rodapé (a aba "Conversa" fica à esquerda). Tocar numa miniatura traz ela de volta.
   */
  _placePhone() {
    const width = number(this.window?.innerWidth, 390);
    const height = number(this.window?.innerHeight, 844);
    const top = 62;
    // música ou vídeo tocando: a faixa deles fica entre o painel e o rodapé (remoteShell.js mede e avisa)
    const bottom = height - 96 - Math.max(0, number(this.window?.KiraRemoteShell?.mediaReserve, 0));
    const open = [...this.records.values()]
      .filter((record) => record.state === 'open' && record.view === this.activeView)
      .sort((left, right) => (right.featuredAt || right.createdAt) - (left.featuredAt || left.createdAt));
    if (!open.length) return [];
    const [featured, ...older] = open;
    const minis = older.slice(0, 3);
    const scale = 0.2;
    const panelWidth = width - 16;
    const rowHeight = minis.length ? Math.round((bottom - top) * 0.62 * scale) + 10 : 0;
    const moving = [featured, ...minis];
    moving.forEach((record) => this._startStageMove(record));
    // cartão do tamanho do conteúdo de um painel do PC, não a tela inteira: no máximo 60% da altura útil, com margem
    const room = bottom - top - rowHeight;
    const maxHeight = room - 8;
    // painel de texto/cartões: exatamente do tamanho do conteúdo (como no PC); vídeo, globo e navegador ficam no cartão fixo
    const natural = this._phoneNaturalHeight(featured);
    const cardHeight = natural ? Math.min(maxHeight, Math.max(140, natural)) : Math.min(maxHeight, Math.max(280, Math.round((bottom - top) * 0.6)));
    Object.assign(featured, { x: 14, y: top + 6, width: width - 28, height: cardHeight, miniScale: 0, staged: true, autoPlaced: true, phoneMax: maxHeight });
    this._watchPhoneContent(featured);
    minis.forEach((record, index) => {
      const miniHeight = Math.round((bottom - top) * 0.62);
      const visual = Math.round(panelWidth * scale);
      Object.assign(record, {
        width: panelWidth, height: miniHeight, miniScale: scale, staged: true, autoPlaced: true,
        x: width - 8 - (index + 1) * (visual + 8) + 8, y: bottom - Math.round(miniHeight * scale),
      });
    });
    older.slice(3).forEach((record) => this.minimize(record.id));
    moving.forEach((record) => this._render(record));
    return moving.map((record) => this.get(record.id));
  }

  /** Altura que o conteúdo pede (barra de título + filhos + margens); 0 quando é mídia que preenche o cartão. */
  _phoneNaturalHeight(record) {
    const content = record.content;
    if (!content?.children?.length || content.querySelector?.('iframe, video, canvas')) return 0;
    let inner = 0;
    for (const child of content.children) inner = Math.max(inner, number(child.offsetTop, 0) + number(child.offsetHeight, 0));
    if (!inner) return 0;
    const titlebar = number(record.element?.querySelector?.('.kui-titlebar')?.offsetHeight, 44);
    return Math.round(titlebar + inner + 20);
  }

  /** O conteúdo chega depois (clima, notícias carregam): reajusta só a altura do cartão em destaque, sem animação de troca. */
  _watchPhoneContent(record) {
    if (record.phoneObserver || typeof this.ResizeObserver !== 'function' || !record.content) return;
    let frame = 0;
    record.phoneObserver = new this.ResizeObserver(() => {
      if (frame) return;
      frame = (this.window?.requestAnimationFrame || ((fn) => this._setTimeout(fn, 16)))(() => {
        frame = 0;
        if (!this._phoneMode() || record.state !== 'open' || record.miniScale) return;
        const natural = this._phoneNaturalHeight(record);
        if (!natural) return;
        const height = Math.min(number(record.phoneMax, natural), Math.max(140, natural));
        if (Math.abs(height - record.height) < 4) return;
        record.height = height;
        this._render(record);
      });
    });
    for (const child of record.content.children) record.phoneObserver.observe?.(child);
  }

  placeMediaPanels() {
    if (this._phoneMode()) return this._placePhone();
    const bounds = this._bounds();
    const mediaRecords = [...this.records.values()].filter((record) => record.state === 'open' && this._isMediaKind(record.kind));
    if (!mediaRecords.length) {
      this.root.dataset.kuiMediaLayout = 'free';
      this._mediaTabs && (this._mediaTabs.hidden = true);
      return [];
    }
    // Também em janela pequena (a janela 2): em abas, o conteúdo novo ficava escondido atrás da aba antiga.
    const stage = this._stageRecords();
    if (stage.length > 1) {
      this.root.dataset.kuiMediaLayout = 'stage';
      const placed = this._placeStage(stage, bounds);
      this._renderMediaTabs();
      return placed;
    }
    // Saiu do palco (sobrou um painel): volta ao tamanho normal, sem a escala de miniatura.
    mediaRecords.forEach((record) => {
      if (!record.staged) return;
      if (record.miniScale || record.autoPlaced) this._startStageMove(record);
      Object.assign(record, { staged: false, miniScale: 0, autoPlaced: false });
    });
    const occupiedRects = [...this.records.values()]
      .filter((record) => record.state === 'open' && !this._isMediaKind(record.kind))
      .map((record) => this._rect(record));
    const placements = placeMediaPanels({
      bounds,
      forbiddenRects: this._forbiddenRects(bounds),
      occupiedRects,
      panels: mediaRecords.map((record) => ({ ...this._snapshot(record), fixed: Boolean(record.autoPlaced) })),
    });
    const compact = placements.some((placement) => placement.tabbed);
    this.root.dataset.kuiMediaLayout = compact ? 'tabs' : 'free';
    if (compact && !mediaRecords.some((record) => record.id === this._mediaTabId && record.state === 'open')) this._mediaTabId = mediaRecords[0].id;
    for (const placement of placements) {
      const record = this.records.get(placement.id);
      if (!record || record.userTouched || record.pinned) continue;
      Object.assign(record, clampToViewport(placement, bounds, { minWidth: MIN_W, minHeight: MIN_H }));
      record.autoPlaced = true;
      this._render(record);
    }
    this._renderMediaTabs();
    return placements;
  }
  _renderMediaTabs() {
    if (!this._mediaTabs) return;
    const mediaRecords = [...this.records.values()].filter((record) => record.state === 'open' && this._isMediaKind(record.kind));
    const compact = this.root.dataset.kuiMediaLayout === 'tabs' && mediaRecords.length > 1;
    this._mediaTabs.hidden = !compact;
    this._mediaTabs.replaceChildren?.();
    if (!compact) {
      mediaRecords.forEach((record) => { if (record.element) record.element.dataset.kuiMediaTabHidden = 'false'; });
      return;
    }
    if (!mediaRecords.some((record) => record.id === this._mediaTabId)) this._mediaTabId = mediaRecords[0]?.id;
    mediaRecords.forEach((record) => {
      if (record.element) record.element.dataset.kuiMediaTabHidden = String(record.id !== this._mediaTabId);
      const button = this.document.createElement('button');
      button.type = 'button';
      button.className = 'kui-media-tab';
      button.textContent = record.title;
      button.setAttribute('aria-selected', String(record.id === this._mediaTabId));
      button.addEventListener?.('click', () => {
        this._mediaTabId = record.id;
        this.records.forEach((item) => this._render(item));
        this._renderMediaTabs();
        this.focus(record.id);
      });
      this._mediaTabs.appendChild(button);
    });
  }
  _intersects(left, right) { return left.x < right.x + right.width && left.x + left.width > right.x && left.y < right.y + right.height && left.y + left.height > right.y; }
  _placeNew(record, bounds, options) {
    if (options.x != null || options.y != null) { this._clampRecord(record, bounds); return; }
    const occupied = [...this.records.values()].filter((other) => other !== record && other.state === 'open').map((other) => this._rect(other));
    const spec = PANEL_DEFAULT_SIZES[record.kind];
    const spot = spec && !record.userTouched
      ? placeSingle(spec, bounds, this._forbiddenRects(bounds), occupied)
      : placeSingle({ width: record.width, height: record.height, minWidth: record.width, minHeight: record.height }, bounds, this._forbiddenRects(bounds), occupied);
    Object.assign(record, spot);
    this._clampRecord(record, bounds);
    if (!record.userTouched) Object.assign(record, clampToViewport(record, bounds, { minWidth: MIN_W, minHeight: MIN_H }));
  }
  /**
   * A primeira posição é calculada ao montar, antes de o layout (fontes, cartão de transcrição, dock) assentar.
   * Um quadro depois, com as fontes prontas, mede de novo e reposiciona se o usuário ainda não mexeu no painel.
   */
  _settleAfterLayout(record) {
    const frame = this.window?.requestAnimationFrame?.bind(this.window);
    if (!frame) return;
    const fontsReady = this.document?.fonts?.ready || Promise.resolve();
    frame(() => fontsReady.then(() => {
      if (record.state !== 'open' || record.userTouched || record.pinned) return;
      if (this._isMediaKind(record.kind)) { this.placeMediaPanels(); return; }
      const before = this._rect(record);
      const bounds = this._bounds();
      this._placeNew(record, bounds, {});
      const moved = Math.abs(record.x - before.x) + Math.abs(record.y - before.y) + Math.abs(record.width - before.width) + Math.abs(record.height - before.height);
      if (moved < 8) { Object.assign(record, before); return; }
      if (record.element?.dataset?.kuiSpawn === 'orb') this._spawnFromOrb(record, bounds);
      this._render(record);
    }).catch(() => {}));
  }
  _render(record) {
    const element = record.element;
    if (!element) return;
    element.dataset.kuiWindowId = record.id;
    element.dataset.kuiKind = record.kind;
    element.dataset.kuiWindowState = record.state;
    element.dataset.kuiCreatedBy = record.createdBy || 'user';
    element.dataset.kuiMedia = this._isMediaKind(record.kind) ? 'true' : 'false';
    element.classList.toggle?.('is-minimized', record.state === 'minimized');
    element.classList.toggle?.('is-maximized', record.state === 'maximized');
    element.classList.toggle?.('is-pinned', record.pinned);
    element.classList.toggle?.('is-closing', record.state === 'closed');
    element.classList.toggle?.('is-view-hidden', record.view !== this.activeView);
    element.hidden = record.state === 'minimized' || element.dataset.kuiMediaTabHidden === 'true';
    element.style.left = `${Math.round(record.x)}px`;
    element.style.top = `${Math.round(record.y)}px`;
    element.style.width = `${Math.round(record.width)}px`;
    element.style.height = `${Math.round(record.height)}px`;
    // Miniatura: o mesmo painel em escala (como no briefing); clicar nela traz de volta ao destaque.
    element.classList.toggle?.('is-mini', Boolean(record.miniScale));
    element.style.transform = record.miniScale ? `scale(${record.miniScale})` : '';
    element.style.zIndex = String(record.zIndex);
    element.setAttribute('aria-label', record.title);
    element.setAttribute('aria-hidden', record.state === 'closed' || record.state === 'minimized' || record.view !== this.activeView ? 'true' : 'false');
    record.titleElement && (record.titleElement.textContent = record.title);
    if (this._isMediaKind(record.kind)) element.style.transitionDuration = reducedMotion(this.window) ? '0ms' : (record.stageMoving ? '' : '150ms');
    const pin = element.querySelector?.('.kui-window-button-pin');
    pin?.setAttribute?.('aria-pressed', record.pinned ? 'true' : 'false');
    this._renderDockItem(record);
    try { this.registry.get(record.kind)?.visibilityChange?.(record.content, record.state === 'open' && record.visible && record.view === this.activeView, this._context(record)); } catch (error) { this._warn('visibilidade do painel falhou.', error); }
  }
  _startDrag(record, event) {
    if (record.state !== 'open' || event.button != null && event.button !== 0 || event.target?.closest?.('button, input, textarea, select')) return;
    this.focus(record.id);
    const start = { pointerX: number(event.clientX), pointerY: number(event.clientY), ...this._rect(record) };
    const pointerId = event.pointerId;
    const element = record.element;
    let dragging = false;
    let frame = 0;
    let last = event;
    // Arrastar liso: o painel anda com 'transform' (composto, sem refazer o layout nem redesenhar a imagem), um quadro
    // por vez; left/top só mudam ao soltar. Antes cada movimento do mouse redesenhava o painel inteiro (janela 2 a ~33/s).
    const paint = () => {
      frame = 0;
      if (!element?.style) return;
      const left = Number.parseFloat(element.style.left) || 0;
      const top = Number.parseFloat(element.style.top) || 0;
      element.style.transform = `translate3d(${Math.round(record.x - left)}px, ${Math.round(record.y - top)}px, 0)`;
      element.classList?.toggle?.('is-leaving', isOutside(this.window, last));
    };
    const move = (next) => {
      const dx = number(next.clientX) - start.pointerX;
      const dy = number(next.clientY) - start.pointerY;
      if (!dragging) {
        if (Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
        dragging = true;
        element?.classList?.add?.('is-dragging');
      }
      last = next;
      record.x = start.x + dx;
      record.y = start.y + dy;
      this._touch(record);
      record.pendingSnap = isOutside(this.window, next) ? null : snapTarget(record.x, record.y, this._bounds());
      this._updateSnapPreview(record.x, record.y);
      this._clampRecord(record, this._bounds());
      if (!frame) frame = this.window?.requestAnimationFrame?.(paint) || 0;
      if (!frame) paint();
      next.preventDefault?.();
    };
    const finish = () => {
      this._removeGesture(record, handle, move, finish, pointerId);
      if (frame) this.window?.cancelAnimationFrame?.(frame);
      frame = 0;
      element?.classList?.remove?.('is-dragging', 'is-leaving');
      const target = record.pendingSnap;
      record.pendingSnap = null;
      this._clearSnapPreview();
      if (target) this._applySnap(record, target);
      else this._render(record);
      // Soltou o painel: a página pergunta ao backend qual janela está sob o ponto da tela. Se for outra janela da Kira
      // (ao lado desta ou por cima dela), o painel passa para ela; senão nada muda.
      if (dragging) this.bus?.emit?.('window:drop', { id: record.id, kind: record.kind, ...screenPoint(this.window, last) });
    };
    const handle = record.titlebar;
    this._addGesture(record, handle, pointerId, move, finish);
  }
  _startResize(record, event, direction) {
    if (record.state !== 'open' || event.button != null && event.button !== 0) return;
    event.preventDefault?.();
    event.stopPropagation?.();
    this.focus(record.id);
    const start = { pointerX: number(event.clientX), pointerY: number(event.clientY), ...this._rect(record) };
    const pointerId = event.pointerId;
    const move = (next) => { Object.assign(record, resizeRect(start, number(next.clientX) - start.pointerX, number(next.clientY) - start.pointerY, direction, this._bounds())); this._touch(record); this._clearSnapPreview(); this._render(record); next.preventDefault?.(); };
    const finish = () => this._removeGesture(record, handle, move, finish, pointerId);
    const handle = event.currentTarget || event.target;
    this._addGesture(record, handle, pointerId, move, finish);
  }
  _addGesture(record, handle, pointerId, move, finish) {
    record.gesture = { handle, move, finish };
    handle?.addEventListener?.('pointermove', move);
    handle?.addEventListener?.('pointerup', finish);
    handle?.addEventListener?.('pointercancel', finish);
    try { handle?.setPointerCapture?.(pointerId); } catch (_) {}
  }
  _removeGesture(record, handle, move, finish, pointerId) {
    handle?.removeEventListener?.('pointermove', move); handle?.removeEventListener?.('pointerup', finish); handle?.removeEventListener?.('pointercancel', finish);
    try { handle?.releasePointerCapture?.(pointerId); } catch (_) {}
    record.gesture = null;
    this._clearSnapPreview();
  }
  _finishGesture(record) { record.gesture?.finish?.(); }
  _applySnap(record, target) {
    const bounds = this._bounds();
    if (target === 'max') { record.restoreRect = this._rect(record); Object.assign(record, { x: 0, y: 0, width: bounds.width, height: bounds.height, state: 'maximized' }); }
    if (target === 'left') Object.assign(record, { x: 0, y: 0, width: bounds.width / 2, height: bounds.height, state: 'open' });
    if (target === 'right') Object.assign(record, { x: bounds.width / 2, y: 0, width: bounds.width / 2, height: bounds.height, state: 'open' });
    this._render(record);
  }
  _updateSnapPreview(x, y) {
    const target = snapTarget(x, y, this._bounds());
    if (!target) { this._clearSnapPreview(); return; }
    const bounds = this._bounds();
    const rect = target === 'max' ? { x: 0, y: 0, width: bounds.width, height: bounds.height } : { x: target === 'right' ? bounds.width / 2 : 0, y: 0, width: bounds.width / 2, height: bounds.height };
    this._snapPreview.hidden = false;
    Object.assign(this._snapPreview.style, { left: `${rect.x}px`, top: `${rect.y}px`, width: `${rect.width}px`, height: `${rect.height}px` });
  }
  _clearSnapPreview() { if (this._snapPreview) this._snapPreview.hidden = true; }
  _readLayout() {
    const empty = { version: LAYOUT_VERSION, viewport: {}, panels: {} };
    try {
      const raw = this.window?.localStorage?.getItem(LAYOUT_STORAGE_KEY);
      const value = raw ? JSON.parse(raw) : null;
      if (!value || value.version !== LAYOUT_VERSION || !value.panels || typeof value.panels !== 'object' || Array.isArray(value.panels)) return empty;
      const panels = {};
      Object.entries(value.panels).slice(0, 32).forEach(([kind, rect]) => {
        if (!/^[a-z0-9_-]{1,64}$/i.test(kind) || !rect || typeof rect !== 'object') return;
        const width = positive(rect.width, 0); const height = positive(rect.height, 0);
        if (width < MIN_W || height < MIN_H) return;
        panels[kind] = { x: number(rect.x), y: number(rect.y), width, height };
      });
      return { version: LAYOUT_VERSION, viewport: value.viewport || {}, panels };
    } catch (_) { return empty; }
  }
  _renderDockItem(record) {
    if (!this._dock) return;
    let button = this._dock.querySelector?.(`[data-kui-dock-id="${record.id}"]`);
    if (record.state !== 'minimized' || record.view !== this.activeView) { button?.remove?.(); this._dock.hidden = ![...this.records.values()].some((item) => item.state === 'minimized' && item.view === this.activeView); return; }
    if (!button) { button = this.document.createElement('button'); button.type = 'button'; button.className = 'kui-dock-item'; button.dataset.kuiAction = 'restore'; button.dataset.kuiDockId = record.id; button.dataset.kuiWindowId = record.id; button.setAttribute('aria-label', `Restaurar ${record.title}`); this._dock.appendChild(button); }
    button.textContent = record.title;
    this._dock.hidden = false;
  }
  _removeDockItem(record) { this._dock?.querySelector?.(`[data-kui-dock-id="${record.id}"]`)?.remove?.(); if (this._dock) this._dock.hidden = ![...this.records.values()].some((item) => item.state === 'minimized' && item.view === this.activeView); }
  _armAutoClose(record) { this._cancelAutoClose(record); if (record.autoCloseMs > 0) record.timer = this._setTimeout(() => { if (!record.pinned && !record.userTouched && record.state !== 'closed') this.close(record.id); }, record.autoCloseMs); }
  _cancelAutoClose(record) { if (record.timer != null) { this._clearTimeout(record.timer); record.timer = null; } }
  _cancelClose(record) { this._cancelAutoClose(record); record.element?.classList?.remove?.('is-closing'); }
  _finalizeClose(record) { if (record.state !== 'closed') return; try { record.cleanup?.(); } catch (_) {} record.phoneObserver?.disconnect?.(); record.element?.remove?.(); this.records.delete(record.id); }
  _setTimeout(fn, ms) { return (this.window?.setTimeout || globalThis.setTimeout)(fn, ms); }
  _clearTimeout(timer) { return (this.window?.clearTimeout || globalThis.clearTimeout)(timer); }
  _snapshot(record) { return { id: record.id, kind: record.kind, view: record.view, title: record.title, state: record.state, visible: record.visible && record.view === this.activeView, x: record.x, y: record.y, width: record.width, height: record.height, createdBy: record.createdBy, pinned: record.pinned, userTouched: record.userTouched, autoCloseMs: record.autoCloseMs, zIndex: record.zIndex, props: { ...record.props } }; }
  _warn(message, error) { try { this.logger?.warn?.(`[Kira UI] ${message}`, error || ''); } catch (_) {} }
}

export const WINDOWING = Object.freeze({ MIN_W, MIN_H, MIN_TITLE_VISIBLE, Z_BASE, Z_PINNED, Z_MAX, CLOSE_ANIMATION_MS, RESIZE_HANDLES });
