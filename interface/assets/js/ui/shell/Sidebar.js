const SCREENS = Object.freeze([
  { id: 'main', label: 'Principal', shortcut: 'Ctrl+1', icon: 'home' },
  { id: 'chat', label: 'Chat', shortcut: 'Ctrl+2', icon: 'chat' },
]);
const SVG_NS = 'http://www.w3.org/2000/svg';
const ICONS = Object.freeze({
  menu: 'M4 6h16M4 12h16M4 18h16',
  home: 'M3 10.5L12 3l9 7.5M5.5 9v11h4.5v-5.5h4V20h4.5V9M12 7.6h.01',
  chat: 'M20.5 15a2.5 2.5 0 0 1-2.5 2.5H8.5L3.5 21V6A2.5 2.5 0 0 1 6 3.5h12A2.5 2.5 0 0 1 20.5 6zM8 10.5h.01M12 10.5h.01M16 10.5h.01',
  bell: 'M18 9a6 6 0 0 0-12 0c0 6.5-2.5 7.5-2.5 8.5h17c0-1-2.5-2-2.5-8.5M10 20.5a2.2 2.2 0 0 0 4 0M12 2v1.2',
  window: 'M3 5h18v14H3zM3 9h18M14 13h4M14 16h4',
  plus: 'M12 5v14M5 12h14',
  dots: 'M12 6h.01M12 12h.01M12 18h.01',
  focus: 'M4 9V5h4M20 9V5h-4M4 15v4h4M20 15v4h-4',
  edit: 'M4 20h4L19 9l-4-4L4 16zM13 7l4 4',
  copy: 'M9 9h11v11H9zM5 15V4h11',
  bolt: 'M13 2L5 14h6l-1 8 8-12h-6z',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  book: 'M2 9l10-5 10 5-10 5zM6 11v5c0 1.7 2.7 3 6 3s6-1.3 6-3v-5M22 9v6',
  house: 'M3 11l9-7.5 9 7.5M5.5 9.5V20h13V9.5M9 15.3a4.2 4.2 0 0 1 6 0M10.6 17.5a1.9 1.9 0 0 1 2.8 0M12 19.4h.01',
});

function icon(documentRef, name) {
  const holder = documentRef.createElement('span');
  holder.className = 'kui-rail-icon';
  holder.dataset.icon = name; // cor própria por item (shell.css)
  holder.setAttribute?.('aria-hidden', 'true');
  if (typeof documentRef.createElementNS !== 'function' || !ICONS[name]) return holder;
  const svg = documentRef.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  const path = documentRef.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', ICONS[name]);
  svg.appendChild(path);
  holder.appendChild(svg);
  return holder;
}

function create(documentRef, tag, className = '', label = '') {
  const node = documentRef.createElement(tag);
  if (className) node.className = className;
  if (label) node.textContent = label;
  return node;
}

function isInput(target) {
  return Boolean(target?.matches?.('input, textarea, select, [contenteditable="true"]'));
}

export function shouldIgnoreShortcut(event) {
  return isInput(event?.target || event?.currentTarget);
}

export class Sidebar {
  constructor({ router, dispatcher, windowManager, documentRef = globalThis.document, windowRef = globalThis.window, logger = globalThis.console } = {}) {
    this.router = router;
    this.dispatcher = dispatcher;
    this.windowManager = windowManager;
    this.document = documentRef;
    this.window = windowRef;
    this.logger = logger;
    this.button = null;
    this.drawer = null;
    this.stageObserver = null;
    this.studiesObserver = null;
    this.opened = false;
    this.listeners = [];
  }

  mount(parent = this.document?.body) {
    if (!parent || this.button) return this;
    const rail = create(this.document, 'aside', 'kui-sidebar');
    rail.setAttribute('aria-label', 'Menu principal da Kira');
    const button = create(this.document, 'button', 'kui-hamburger');
    button.type = 'button';
    button.setAttribute('aria-label', 'Abrir menu da Kira');
    button.setAttribute('aria-expanded', 'false');
    button.appendChild(icon(this.document, 'menu'));
    button.addEventListener('click', (event) => { event.stopPropagation(); this.toggle(); });
    rail.appendChild(button);
    // Em Estudos, escolher outro destino do menu sai de Estudos (o ☰ não fica preso por cima da tela cheia).
    rail.addEventListener('click', (event) => {
      const hit = event.target.closest?.('.kui-sidebar-action');
      if (!hit || !this.document?.body?.classList?.contains('kira-studies-open')) return;
      if (hit.dataset.kuiStudies !== undefined || /estudo/i.test(hit.textContent || '')) { this.close(); return; }
      globalThis.KiraStudies?.close?.();
    }, true);
    rail.appendChild(this.buildScreens());
    rail.appendChild(this.buildAssistant());
    rail.appendChild(this.buildHome());
    rail.appendChild(this.buildStudies());
    rail.appendChild(this.buildWindows());
    parent.appendChild(rail);
    this.button = button;
    this.drawer = rail;
    this.bindUiStage();
    this.on(this.document, 'keydown', (event) => this.onKeyDown(event));
    this.on(this.document, 'click', (event) => {
      // composedPath é calculado antes de qualquer redesenho: um botão removido da lista não conta como clique "fora".
      const inside = event.composedPath?.().includes(this.drawer) || this.drawer.contains?.(event.target);
      if (!this.opened || inside) return;
      this.close();
    });
    return this;
  }

  bindUiStage(appShell = this.document?.getElementById?.('app-shell')) {
    const sync = () => {
      const isMain = appShell?.dataset?.uiStage === 'main';
      const studiesOpen = Boolean(this.document?.body?.classList?.contains('kira-studies-open'));
      // Estudos usa o mesmo ☰ do Chat: o trilho encolhe para só o botão (CSS) e abre por cima ao tocar.
      const available = isMain || studiesOpen;
      if (!available) this.close();
      if (this.drawer) {
        // O workspace de Estudos ocupa a tela inteira. Enquanto ele estiver
        // aberto, o rail não pode permanecer por cima dele e capturar os
        // cliques da navegação interna.
        this.drawer.hidden = !available;
        this.drawer.setAttribute('aria-hidden', String(!available));
      }
    };
    sync();
    if (appShell && typeof MutationObserver !== 'undefined') {
      this.stageObserver = new MutationObserver(sync);
      this.stageObserver.observe(appShell, { attributes: true, attributeFilter: ['data-ui-stage'] });
      if (this.document?.body) {
        this.stageObserver.observe(this.document.body, { attributes: true, attributeFilter: ['class'] });
      }
    }
  }

  on(target, type, handler) {
    target?.addEventListener?.(type, handler);
    this.listeners.push({ target, type, handler });
  }

  buildScreens() {
    const section = this.section('Principal');
    SCREENS.forEach((screen) => {
      const button = this.actionButton(screen.label, screen.shortcut, () => this.go(screen.id), screen.icon);
      button.dataset.kuiScreen = screen.id;
      section.appendChild(button);
    });
    return section;
  }

  buildAssistant() {
    const section = this.section('Lembretes');
    const button = this.actionButton('Lembretes e alarmes', '', () => this.openReminders(), 'bell');
    button.dataset.kuiAssistant = 'reminders';
    section.appendChild(button);
    return section;
  }

  buildHome() {
    const section = this.section('Casa');
    const button = this.actionButton('Casa inteligente', '', () => this.openHome(), 'house');
    button.dataset.kuiAssistant = 'smart_home';
    section.appendChild(button);
    return section;
  }

  buildStudies() {
    const section = this.section('Estudos');
    const button = this.actionButton('Estudos', '', () => {
      if (this.window?.KiraStudies?.open) this.window.KiraStudies.open();
      else {
        const existing = this.windowManager?.serialize?.().find((item) => item.kind === 'tutor');
        if (existing) this.windowManager.restore?.(existing.id);
        else this.dispatcher?.dispatch?.({ type: 'open_panel', kind: 'tutor', id: 'tutor', title: 'Estudos', props: { mode: 'dashboard', load: true } });
      }
      this.close();
    }, 'book');
    button.dataset.kuiAssistant = 'tutor';
    section.appendChild(button);
    return section;
  }

  /** Lista dos aparelhos do Wi-Fi de casa (views/SmartHomePanel.js). */
  openHome() {
    const existing = this.windowManager?.serialize?.().find((item) => item.kind === 'smart_home');
    if (existing) this.windowManager.restore?.(existing.id);
    else this.dispatcher?.dispatch?.({ type: 'open_panel', kind: 'smart_home', title: 'Casa inteligente', view: 'main' });
    this.close();
  }

  openReminders() {
    const existing = this.windowManager?.serialize?.().find((item) => item.kind === 'reminders');
    if (existing) this.windowManager.restore?.(existing.id);
    else this.dispatcher?.dispatch?.({ type: 'open_panel', kind: 'reminders', title: 'Lembretes e alarmes', view: 'main' });
    this.close();
  }

  buildWindows() {
    const section = this.section('Janela');
    const add = this.actionButton('Nova janela', '', () => this.createWindow(), 'plus');
    add.dataset.kuiWindow = 'new';
    this.windowList = create(this.document, 'div', 'kui-window-list');
    this.windowData = [];
    this.windowUi = { menu: null, renaming: null };
    section.append(add, this.windowList);
    this.drawWindowList();
    return section;
  }

  async windowApi(method, path, body) {
    const response = await this.window?.fetch?.(path, { method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    if (!response?.ok) throw new Error(`HTTP ${response?.status}`);
    return response.json();
  }

  async refreshWindows() {
    try {
      const body = await this.windowApi('GET', '/api/ui/windows');
      this.windowData = body.profiles || [];
      this.windowStates = Object.fromEntries((body.windows || []).map((window) => [window.windowId, window.state]));
      this.drawWindowList();
    } catch (error) {
      this.logger?.debug?.('[Kira UI] lista de janelas indisponível.', error);
    }
  }

  /** Executa a ação e redesenha a lista; o menu lateral nunca fecha por causa disso. */
  async act(action) {
    try { await action(); } catch (error) { this.logger?.warn?.('[Kira UI] ação de janela falhou.', error); }
    await this.refreshWindows();
  }

  async createWindow() {
    await this.act(async () => {
      const created = await this.windowApi('POST', '/api/ui/profiles', {});
      this.windowUi.renaming = created.profile.id;
    });
  }

  miniButton(label, iconName, handler, className = '') {
    const button = create(this.document, 'button', `kui-window-mini ${className}`.trim());
    button.type = 'button';
    button.appendChild(icon(this.document, iconName));
    button.appendChild(create(this.document, 'span', '', label));
    button.addEventListener('click', handler);
    return button;
  }

  windowCard(profile) {
    const ui = this.windowUi;
    const card = create(this.document, 'div', `kui-window-card${profile.open ? ' is-open' : ''}`);
    card.dataset.kuiProfile = profile.id;
    const head = create(this.document, 'div', 'kui-window-head');
    if (ui.renaming === profile.id) {
      const input = create(this.document, 'input', 'kui-window-input');
      input.value = profile.name;
      input.maxLength = 40;
      input.setAttribute('aria-label', 'Nome da janela');
      let finished = false;
      const finish = (save) => {
        if (finished) return;
        finished = true;
        ui.renaming = null;
        const name = input.value.trim();
        this.act(() => (save && name && name !== profile.name ? this.windowApi('PATCH', `/api/ui/profiles/${profile.id}`, { name }) : Promise.resolve()));
      };
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); finish(true); }
        else if (event.key === 'Escape') { event.stopPropagation(); finish(false); }
      });
      input.addEventListener('blur', () => finish(true));
      head.appendChild(input);
      this.window?.setTimeout?.(() => { input.focus?.(); input.select?.(); }, 0);
    } else {
      head.appendChild(create(this.document, 'span', 'kui-window-name', profile.name));
    }
    const state = profile.open ? (this.windowStates?.[profile.windowId] === 'minimizada' ? 'minimizada' : 'aberta') : 'fechada';
    head.appendChild(create(this.document, 'span', 'kui-window-state', state));
    const more = create(this.document, 'button', 'kui-window-more');
    more.type = 'button';
    more.title = 'Ações da janela';
    more.setAttribute('aria-label', `Ações de ${profile.name}`);
    more.setAttribute('aria-expanded', String(ui.menu === profile.id));
    more.appendChild(icon(this.document, 'dots'));
    more.addEventListener('click', () => { ui.menu = ui.menu === profile.id ? null : profile.id; this.drawWindowList(); });
    head.appendChild(more);
    card.appendChild(head);
    if (ui.menu === profile.id) {
      const menu = create(this.document, 'div', 'kui-window-menu');
      menu.setAttribute('role', 'menu');
      const close = () => { ui.menu = null; };
      menu.appendChild(this.miniButton(profile.open ? 'Focar' : 'Abrir', 'focus', () => { close(); this.act(() => this.windowApi('POST', '/api/ui/windows', { profileId: profile.id })); }));
      menu.appendChild(this.miniButton('Renomear', 'edit', () => { close(); ui.renaming = profile.id; this.drawWindowList(); }));
      menu.appendChild(this.miniButton('Duplicar', 'copy', () => { close(); this.act(() => this.windowApi('POST', `/api/ui/profiles/${profile.id}/duplicate`)); }));
      menu.appendChild(this.miniButton(`Início automático: ${profile.openOnStart ? 'ligado' : 'desligado'}`, 'bolt', () => this.act(() => this.windowApi('PATCH', `/api/ui/profiles/${profile.id}`, { openOnStart: !profile.openOnStart })), profile.openOnStart ? 'is-on' : ''));
      const remove = this.miniButton('Excluir', 'trash', () => {
        if (!remove.dataset.confirm) {
          remove.dataset.confirm = '1';
          remove.lastChild.textContent = 'Confirmar exclusão?';
          return;
        }
        close();
        this.act(() => this.windowApi('DELETE', `/api/ui/profiles/${profile.id}`));
      }, 'is-danger');
      menu.appendChild(remove);
      card.appendChild(menu);
    }
    return card;
  }

  drawWindowList() {
    // Só as janelas criadas: a principal é esta, não precisa estar na lista.
    const list = this.windowList;
    list.replaceChildren();
    if (!this.windowData.length) list.appendChild(create(this.document, 'p', 'kui-window-empty', 'Nenhuma janela criada ainda.'));
    this.windowData.forEach((profile) => list.appendChild(this.windowCard(profile)));
  }

  section(title) {
    const section = create(this.document, 'section', 'kui-sidebar-section');
    section.setAttribute('aria-label', title);
    section.appendChild(create(this.document, 'h3', '', title));
    return section;
  }

  actionButton(label, hint, action, iconName) {
    const button = create(this.document, 'button', 'kui-sidebar-action');
    button.type = 'button';
    button.title = label;
    button.setAttribute('aria-label', label);
    button.appendChild(icon(this.document, iconName));
    button.appendChild(create(this.document, 'span', 'kui-sidebar-action-name', label));
    button.appendChild(create(this.document, 'span', 'kui-sidebar-action-meta', hint));
    button.addEventListener('click', action);
    return button;
  }

  go(viewId) {
    this.router?.go?.(viewId);
    this.close();
  }

  toggle() { return this.opened ? this.close() : this.open(); }

  open() {
    if (!this.drawer) return;
    this.opened = true;
    this.drawer.classList.add('is-open');
    this.refreshWindows();
    this.button.classList.add('is-open');
    this.button.setAttribute('aria-label', 'Recolher menu da Kira');
    this.button.setAttribute('aria-expanded', 'true');
  }

  close() {
    if (!this.drawer) return;
    this.opened = false;
    this.drawer.classList.remove('is-open');
    this.button.classList.remove('is-open');
    this.button.setAttribute('aria-label', 'Abrir menu da Kira');
    this.button.setAttribute('aria-expanded', 'false');
  }

  onKeyDown(event) {
    if (event.key === 'Escape') {
      if (!shouldIgnoreShortcut(event)) { this.close(); event.preventDefault?.(); }
      return;
    }
    if (shouldIgnoreShortcut(event)) return;
    if (!(event.ctrlKey || event.metaKey)) return;
    const key = String(event.key || '').toLowerCase();
    if (key === '1' || key === '2' || key === '3') {
      const screen = SCREENS[Number(key) - 1];
      if (!screen) return;
      event.preventDefault?.();
      this.go(screen.id);
    }
  }

  destroy() {
    this.stageObserver?.disconnect?.();
    this.stageObserver = null;
    this.listeners.forEach(({ target, type, handler }) => target?.removeEventListener?.(type, handler));
    this.listeners = [];
    this.drawer?.remove?.();
    this.button = this.drawer = null;
  }
}

export const SIDEBAR_SCREENS = SCREENS;
