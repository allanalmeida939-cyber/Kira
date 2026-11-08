// Painel de vídeo no canto: o player do YouTube da aba Música aparece aqui, com a barra do próprio YouTube.
// Mostra quando um vídeo do YouTube está tocando/pausado/carregando; some com a aba Música aberta (o som continua);
// minimizado, o painel some e o cartão "tocando agora" (nowPlaying.js, com o estilo escolhido na aba Aparência) fica
// no lugar, com o botão para mostrar o vídeo de novo. Tela cheia leva a janela da Kira para o monitor principal. Por voz: control_music
// (pausar, volume, tela cheia, minimizar, fechar) e tocar_youtube.

/** Próximo estado do painel a partir do evento do player ({state, source, title, code}). Erro do player some com o
 *  painel: vídeo bloqueado fora do YouTube vira áudio do YouTube (sem vídeo) e o recado fica na aba Música. */
export function dockView(detail = {}, previous = { open: false, closedByUser: false }) {
  const state = String(detail.state || '');
  const youtube = String(detail.source || 'youtube_embed') === 'youtube_embed';
  let { closedByUser } = previous;
  if (state === 'loading' || state === 'playing') closedByUser = false; // pedido novo reabre o painel
  const active = youtube && ['playing', 'paused', 'loading'].includes(state);
  return { open: active && !closedByUser, closedByUser };
}

function el(doc, tag, className = '', text = '') {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function createVideoDock({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  const doc = documentRef;
  const win = windowRef;
  let view = { open: false, closedByUser: false };
  let fullscreen = false;
  let minimized = false;
  let lastTitle = '';
  const drawer = doc.getElementById('music-drawer');
  const drawerOpen = () => Boolean(drawer?.classList.contains('open'));

  const box = el(doc, 'div', 'kira-video-dock is-hidden');
  box.setAttribute('role', 'region');
  box.setAttribute('aria-label', 'Vídeo do YouTube');
  const head = el(doc, 'div', 'kira-video-dock-head');
  const title = el(doc, 'span', 'kira-video-dock-title');
  const button = (label, glyph, action) => {
    const node = el(doc, 'button', '', glyph);
    node.type = 'button';
    node.title = label;
    node.setAttribute('aria-label', label);
    node.addEventListener('click', action);
    return node;
  };
  const fullButton = button('Tela cheia', '⛶', () => api.setFullscreen(!fullscreen));
  const minButton = button('Minimizar', '▁', () => api.setMinimized(!minimized));
  head.append(title, minButton, fullButton, button('Fechar', '✕', () => api.close()));
  const body = el(doc, 'div', 'kira-video-dock-body');
  box.append(head, body);
  // O mesmo player da aba Música: mover antes de ele ser criado (o iframe nasce já aqui dentro).
  const host = doc.getElementById('music-youtube-player-host');
  if (host) body.appendChild(host);
  doc.body.appendChild(box);

  const render = () => {
    const folded = minimized && !fullscreen;
    const visible = view.open && (fullscreen || !drawerOpen()) && !folded;
    box.className = `kira-video-dock${visible ? '' : ' is-hidden'}${fullscreen ? ' is-fullscreen' : ''}${folded ? ' is-minimized' : ''}`;
    fullButton.textContent = fullscreen ? '🗗' : '⛶';
    fullButton.title = fullscreen ? 'Sair da tela cheia' : 'Tela cheia';
    win.dispatchEvent?.(new win.CustomEvent('kira:video-dock', { detail: { open: view.open, minimized: folded } }));
  };

  const api = {
    element: box,
    isOpen: () => view.open,
    isMinimized: () => view.open && minimized && !fullscreen,
    update(detail) {
      view = dockView(detail, view);
      const newTitle = String(detail?.title || '');
      if (newTitle && newTitle !== lastTitle && ['loading', 'playing'].includes(String(detail?.state))) {
        // música escolhida na própria aba Música: ao fechar a aba o vídeo fica só como barrinha no canto
        minimized = drawerOpen();
        lastTitle = newTitle;
      }
      if (newTitle) title.textContent = newTitle;
      if (!view.open && fullscreen) api.setFullscreen(false);
      render();
      return view;
    },
    async setFullscreen(on) {
      fullscreen = Boolean(on) && view.open;
      render();
      try {
        await win.fetch?.('/api/window/fullscreen', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on: fullscreen }),
        });
      } catch (_) { /* sem a janela em tela cheia o painel ainda cobre a Kira inteira */ }
      return { ok: true, fullscreen };
    },
    setMinimized(on) {
      minimized = Boolean(on);
      if (minimized && fullscreen) api.setFullscreen(false);
      render();
      return { ok: true, minimized };
    },
    close() {
      win.KiraUiControl?.musicCommand?.('stop');
      view = { open: false, closedByUser: true };
      if (fullscreen) api.setFullscreen(false);
      render();
      return { ok: true };
    },
  };
  win.addEventListener?.('kira:music-state', (event) => api.update(event.detail || {}));
  if (drawer && win.MutationObserver) new win.MutationObserver(render).observe(drawer, { attributes: true, attributeFilter: ['class'] });
  win.addEventListener?.('keydown', (event) => { if (fullscreen && event.key === 'Escape') api.setFullscreen(false); });
  render();
  return api;
}

if (globalThis.document?.getElementById?.('music-youtube-player-host') && globalThis.window && !globalThis.window.KiraVideoDock) {
  globalThis.window.KiraVideoDock = createVideoDock();
}
