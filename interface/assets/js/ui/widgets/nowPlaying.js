// Mini player no canto: com música carregada e a aba Música fechada, mostra capa, nome, artista, tempo e controles.
// Também é o "minimizado" do vídeo do YouTube (videoDock.js): o vídeo segue tocando escondido e o cartão ganha o botão
// para mostrar o vídeo de novo. Espelha o player da aba (mesmos elementos, sem segundo estado) e chama as funções dela.
// O visual vem da aba Aparência (html[data-kira-player]: neon, cápsula, vinil, onda); animações só com transform.

function el(doc, tag, className = '', text = '') {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

const SAFE_THUMB = /^(https:\/\/|\/api\/|data:image\/)/;

export function createNowPlaying({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  const doc = documentRef;
  const $ = (id) => doc.getElementById(id);
  const box = el(doc, 'div', 'kira-now-playing');
  box.hidden = true;
  box.setAttribute('role', 'region');
  box.setAttribute('aria-label', 'Tocando agora');
  const art = el(doc, 'div', 'kira-np-art');
  const cover = el(doc, 'img');
  cover.alt = '';
  art.append(el(doc, 'span', 'kira-np-disc'), cover);
  const text = el(doc, 'div', 'kira-now-playing-text');
  text.title = 'Abrir a aba Música';
  const title = el(doc, 'div', 'kira-now-playing-title');
  const artist = el(doc, 'div', 'kira-now-playing-artist');
  const time = el(doc, 'div', 'kira-now-playing-time');
  const bar = el(doc, 'div', 'kira-now-playing-bar');
  const fill = el(doc, 'span');
  bar.appendChild(fill);
  text.append(title, artist, time, bar);
  const eq = el(doc, 'div', 'kira-np-eq');
  eq.setAttribute('aria-hidden', 'true');
  for (let i = 0; i < 5; i += 1) eq.appendChild(el(doc, 'i'));
  const buttons = el(doc, 'div', 'kira-now-playing-buttons');
  const button = (label, glyph, action, className = '') => {
    const node = el(doc, 'button', className, glyph);
    node.type = 'button';
    node.title = label;
    node.setAttribute('aria-label', label);
    node.addEventListener('click', action);
    return node;
  };
  const call = (name) => () => windowRef[name]?.();
  let played = false; // algo tocou nesta abertura da Kira (faixa só restaurada da vez anterior não abre o cartão)
  let dismissed = ''; // faixa dispensada no ✕: o cartão some até tocar de novo
  const play = button('Tocar ou pausar', '▶', call('musicTogglePlay'), 'is-main');
  const video = button('Mostrar o vídeo', '▣', () => windowRef.KiraVideoDock?.setMinimized?.(false), 'is-video');
  const stop = button('Parar', '✕', () => {
    dismissed = String($('music-now-title')?.textContent || '').trim();
    if (windowRef.KiraVideoDock?.isOpen?.()) windowRef.KiraVideoDock.close();
    else windowRef.KiraUiControl?.musicCommand?.('stop');
    sync();
  }, 'is-stop');
  buttons.append(button('Anterior', '⏮', call('musicPrev')), play, button('Próxima', '⏭', call('musicNext')), video, stop);
  box.append(art, text, eq, buttons);
  text.addEventListener('click', () => windowRef.toggleDrawer?.('music'));

  const sync = () => {
    const name = String($('music-now-title')?.textContent || '').trim();
    const drawerOpen = Boolean($('music-drawer')?.classList.contains('open'));
    const pauseIcon = doc.querySelector('#music-play-btn .mus-icon-pause');
    const playing = Boolean(pauseIcon) && pauseIcon.style.display !== 'none';
    const current = String($('music-time-current')?.textContent || '00:00').trim();
    if (playing) {
      played = true;
      dismissed = '';
    }
    // Faixa só "carregada" da sessão anterior (parada) não abre o mini player; pausada nesta sessão, abre.
    const active = Boolean(name) && name !== 'Nada tocando' && (playing || (played && current !== '00:00')) && name !== dismissed;
    // Com qualquer janela de painel aberta (Configurações, WhatsApp...) o cartão sai da frente.
    // Vídeo do YouTube aberto no canto: o painel já é o player; minimizado, este cartão fica no lugar dele.
    const dock = windowRef.KiraVideoDock;
    const videoOpen = Boolean(dock?.isOpen?.());
    const videoMinimized = videoOpen && Boolean(dock?.isMinimized?.());
    const panelOpen = Boolean(doc.body?.classList?.contains?.('drawer-open'));
    box.hidden = !active || drawerOpen || panelOpen || (videoOpen && !videoMinimized);
    if (box.hidden) return;
    box.className = `kira-now-playing${playing ? ' is-playing' : ''}`;
    title.textContent = name;
    // "Canal • YouTube" da aba: no cartão só o canal.
    artist.textContent = String($('music-now-sub')?.textContent || '').split('•')[0].trim();
    time.textContent = `${current} / ${$('music-time-total')?.textContent || '00:00'}`;
    fill.style.width = `${Math.min(100, Math.max(0, Number($('music-progress')?.value || 0) / 10))}%`;
    const thumb = String($('music-now-thumb')?.getAttribute('src') || '');
    if (SAFE_THUMB.test(thumb) && cover.getAttribute('src') !== thumb) cover.src = thumb;
    play.textContent = playing ? '⏸' : '▶';
    video.hidden = !videoMinimized;
  };

  const watched = ['music-now-title', 'music-now-sub', 'music-time-current', 'music-now-thumb', 'music-drawer', 'music-play-btn'];
  const observer = new windowRef.MutationObserver(sync);
  for (const id of watched) {
    const node = $(id);
    if (node) observer.observe(node, { childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ['class', 'src', 'style'] });
  }
  if (doc.body) observer.observe(doc.body, { attributes: true, attributeFilter: ['class'] });
  windowRef.addEventListener?.('kira:video-dock', sync);
  doc.body.appendChild(box);
  sync();
  return { element: box, sync, destroy: () => { observer.disconnect(); box.remove(); } };
}

if (globalThis.document?.getElementById?.('music-drawer') && globalThis.window && !globalThis.window.KiraNowPlaying) {
  globalThis.window.KiraNowPlaying = createNowPlaying();
}
