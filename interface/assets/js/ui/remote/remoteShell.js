// Casco do celular (só na página aberta pelo portão, window.KIRA_REMOTE): dois modos e um menu.
// - Voz (padrão): orbe no centro, transcrição recolhida numa aba ("Conversa") que abre por cima; painel que a Kira abre
//   recolhe a transcrição e aparece no meio (o novo em destaque, os anteriores em miniatura ao lado: WindowManager).
// - Chat: só a conversa, em tela cheia, por texto (como um chat comum); sem orbe e sem microfone.
// O modo fica guardado neste aparelho. Também no menu: instalar o app (convite do Chrome) e ligar as notificações
// (Web Push: services/web_push.py, interface/sw.js). Tudo por textContent.

const MODE_KEY = 'kira.phone.mode';
const MODES = { voice: 'Voz', chat: 'Chat' };

function el(doc, tag, className = '', text = '') {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function createRemoteShell({ documentRef = globalThis.document, windowRef = globalThis.window, storage = globalThis.localStorage } = {}) {
  const doc = documentRef;
  const win = windowRef;
  const body = doc.body;
  let mode = 'voice';
  try { mode = MODES[storage?.getItem(MODE_KEY)] ? storage.getItem(MODE_KEY) : 'voice'; } catch (_) { /* sem armazenamento: voz */ }

  // transcrição: no modo voz ela "flutua" por cima e não ocupa espaço dos painéis (WindowManager ignora data-kui-floating)
  const transcript = doc.getElementById('kira-transcript-panel');
  if (transcript) transcript.dataset.kuiFloating = 'true';

  const menuButton = el(doc, 'button', 'phone-menu-button');
  menuButton.type = 'button';
  menuButton.setAttribute('aria-label', 'Menu');
  menuButton.setAttribute('aria-expanded', 'false');
  menuButton.append(el(doc, 'span'), el(doc, 'span'), el(doc, 'span'));

  const menu = el(doc, 'nav', 'phone-menu');
  menu.setAttribute('aria-label', 'Menu da Kira');
  menu.hidden = true;
  const title = el(doc, 'div', 'phone-menu-title', 'KIRA');
  const items = [
    ['voice', 'Principal', ''],
    ['chat', 'Chat', 'Só texto: o microfone desliga'],
    ['reminders', 'Lembretes e alarmes', ''],
    ['home', 'Casa inteligente', ''],
    ['whatsapp', 'WhatsApp', ''],
    ['browser', 'Internet', ''],
    ['music', 'Música', ''],
    ['config', 'Ajustes', 'Só deste celular'],
    ['notify', 'Ativar notificações', 'A Kira avisa no celular, mesmo com o app fechado'],
    ['install', 'Instalar o app da Kira', 'Ícone na tela inicial, abre como aplicativo'],
    ['switch', 'Trocar de perfil', 'Entrar com outro perfil neste aparelho'],
  ];
  const buttons = {};
  // Neon líquido (como o Trilho do PC): um bloco de cor escorrega até o item ativo
  const blob = el(doc, 'span', 'phone-menu-blob');
  menu.append(title, blob);
  for (const [key, label, help] of items) {
    const item = el(doc, 'button', 'phone-menu-item');
    item.type = 'button';
    item.dataset.key = key;
    item.append(el(doc, 'span', 'phone-menu-label', label));
    if (help) item.append(el(doc, 'span', 'phone-menu-help', help));
    item.addEventListener('click', () => choose(key));
    buttons[key] = item;
    menu.append(item);
  }
  const backdrop = el(doc, 'div', 'phone-menu-backdrop');
  backdrop.hidden = true;
  backdrop.addEventListener('click', () => toggleMenu(false));

  const tab = el(doc, 'button', 'phone-transcript-tab', 'Conversa');
  tab.type = 'button';
  tab.setAttribute('aria-expanded', 'false');
  tab.addEventListener('click', () => setTranscript(!body.classList.contains('phone-transcript-open')));

  // rodapé: tela do celular · microfone · câmera do celular (a Kira vê o que você mostra)
  const vision = (key, label) => {
    const button = el(doc, 'button', `phone-foot phone-foot-${key}`);
    button.type = 'button';
    button.setAttribute('aria-label', label);
    button.title = label;
    button.addEventListener('click', async () => {
      const voice = win.KiraRemoteVoice;
      const result = await voice?.setVision?.(voice.seeing === key ? null : key);
      [footScreen, footCam].forEach((b) => b.classList.toggle('is-on', voice?.seeing === b.dataset.key));
      if (result?.error === 'screen_unsupported') footScreen.classList.add('is-blocked');
    });
    button.dataset.key = key;
    return button;
  };
  const footScreen = vision('screen', 'Mostrar a tela do celular');
  const footCam = vision('camera', 'Câmera do celular: a Kira vê');
  body.append(menuButton, backdrop, menu, tab, footScreen, footCam);

  function placeBlob(item) {
    if (!item || menu.hidden) return;
    blob.style.transform = `translateY(${item.offsetTop}px)`;
    blob.style.height = `${item.offsetHeight}px`;
  }

  function toggleMenu(open) {
    menu.hidden = !open;
    backdrop.hidden = !open;
    menuButton.setAttribute('aria-expanded', open ? 'true' : 'false');
    body.classList.toggle('phone-menu-open', open);
    if (open) placeBlob(buttons[mode]);
  }

  function setTranscript(open) {
    body.classList.toggle('phone-transcript-open', open);
    tab.setAttribute('aria-expanded', open ? 'true' : 'false');
    tab.textContent = open ? 'Fechar conversa' : 'Conversa';
    if (open) {
      const list = doc.getElementById('left-transcription-messages');
      if (list) list.scrollTop = list.scrollHeight;
    }
  }

  function setMode(next) {
    mode = MODES[next] ? next : 'voice';
    body.classList.toggle('phone-mode-chat', mode === 'chat');
    body.classList.toggle('phone-mode-voice', mode === 'voice');
    Object.entries(buttons).forEach(([key, button]) => button.classList.toggle('is-active', key === mode));
    placeBlob(buttons[mode]);
    if (mode === 'chat') {
      win.KiraRemoteVoice?.micOn && win.KiraRemoteVoice.setMic(false); // chat é só texto
      setTranscript(false);
      const list = doc.getElementById('left-transcription-messages');
      if (list) list.scrollTop = list.scrollHeight;
    }
    try { storage?.setItem(MODE_KEY, mode); } catch (_) { /* segue */ }
  }

  function choose(key) {
    placeBlob(buttons[key]);
    toggleMenu(false);
    if (MODES[key]) { setMode(key); return; }
    if (key === 'config') { win.toggleDrawer?.('config'); return; }
    if (key === 'whatsapp') { win.openWhatsappPanel?.(); return; }
    if (key === 'browser') { win.openBrowserPanel?.(); return; }
    if (key === 'music') { win.openMusicPanel?.(); return; }
    if (key === 'reminders') win.toggleDrawer?.('reminders');
    if (key === 'home') win.KiraDispatch?.({ type: 'open_panel', kind: 'smart_home', title: 'Casa inteligente', view: 'main' });
    if (key === 'camera' || key === 'screen') {
      const voice = win.KiraRemoteVoice;
      voice?.setVision?.(voice.seeing === key ? null : key);
      return;
    }
    if (key === 'notify') enableNotifications();
    if (key === 'install') install();
    if (key === 'switch') win.location?.assign?.('/sair');
  }

  const status = (text) => win.setComposerStatus?.(text);
  const help = (key, text) => { const node = buttons[key]?.querySelector('.phone-menu-help'); if (node) node.textContent = text; };

  function keyBytes(base64) {
    const padded = (base64 + '='.repeat((4 - (base64.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/');
    return Uint8Array.from(win.atob(padded), (char) => char.charCodeAt(0));
  }

  async function subscribe(prompt) {
    const nav = win.navigator;
    if (!nav?.serviceWorker || !win.PushManager || !win.Notification) {
      if (prompt) status('Este navegador não recebe notificações: abra pelo Chrome e instale o app.');
      return false;
    }
    if (!win.isSecureContext) {
      if (prompt) status('Notificações só pelo link seguro (https).');
      return false;
    }
    const permission = prompt ? await win.Notification.requestPermission() : win.Notification.permission;
    if (permission !== 'granted') {
      if (prompt) status('Sem permissão de notificação: libere nas configurações do navegador.');
      return false;
    }
    const registration = await nav.serviceWorker.register('/sw.js');
    await nav.serviceWorker.ready;
    const { key } = await (await win.fetch('/api/phone/push/key', { cache: 'no-store' })).json();
    const current = await registration.pushManager.getSubscription();
    const subscription = current || await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
    const saved = await (await win.fetch('/api/phone/push/subscribe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ subscription: subscription.toJSON() }),
    })).json();
    help('notify', saved?.ok ? 'Ligadas. Toque para mandar um teste.' : 'Não consegui ligar agora.');
    return Boolean(saved?.ok);
  }

  async function enableNotifications() {
    try {
      if (await subscribe(true)) {
        const response = await win.fetch('/api/phone/push/test', { method: 'POST' });
        const result = await response.json().catch(() => ({}));
        // "aceito pelo serviço de push" não é "apareceu": quem confirma é o próprio aparelho
        status(response.ok && result.ok
          ? 'Notificações ligadas: teste aceito pelo serviço de push. Se não aparecer em alguns segundos, confira a permissão do navegador.'
          : `Notificações ligadas, mas o teste não saiu${result.error ? `: ${result.error}` : ''}.`);
      }
    } catch (_) {
      status('Não consegui ligar as notificações agora.');
    }
  }

  // convite de instalação do Chrome (Android); no iPhone é pelo Safari
  let installPrompt = null;
  win.addEventListener?.('beforeinstallprompt', (event) => { event.preventDefault(); installPrompt = event; help('install', 'Pronto para instalar: toque aqui.'); });
  win.addEventListener?.('appinstalled', () => { installPrompt = null; help('install', 'Instalado. Abra pelo ícone da Kira.'); });
  async function install() {
    if (installPrompt) {
      installPrompt.prompt();
      installPrompt = null;
      return;
    }
    const standalone = win.matchMedia?.('(display-mode: standalone)')?.matches;
    status(standalone ? 'Você já está no app da Kira.'
      : /iPhone|iPad/.test(String(win.navigator?.userAgent || '')) ? 'No iPhone: toque em Compartilhar e em "Adicionar à Tela de Início".'
        : 'No Chrome: menu ⋮ e "Instalar app" (ou "Adicionar à tela inicial").');
  }

  menuButton.addEventListener('click', () => toggleMenu(menu.hidden));
  win.addEventListener?.('kira:phone-vision', (event) => {
    const seeing = event?.detail?.seeing;
    buttons.camera?.classList.toggle('is-active', seeing === 'camera');
    buttons.screen?.classList.toggle('is-active', seeing === 'screen');
    const label = buttons.camera?.querySelector('.phone-menu-label');
    if (label) label.textContent = seeing === 'camera' ? 'Desligar a câmera' : 'Câmera: a Kira vê';
  });

  // painel novo no modo voz: a transcrição sai da frente para o painel aparecer inteiro
  const root = doc.getElementById('kira-ui-root');
  let open = 0;
  const countPanels = () => (root ? [...root.querySelectorAll('.kui-window')].filter((node) => !node.classList.contains('is-closing') && !node.hidden).length : 0);
  if (root && win.MutationObserver) {
    new win.MutationObserver(() => {
      const now = countPanels();
      if (now > open && mode === 'voice') setTranscript(false);
      body.classList.toggle('phone-has-panel', now > 0);
      open = now;
    }).observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
  }

  // faixa de mídia: vídeo ou "tocando agora" ficam acima do rodapé; os painéis e a conversa param acima dela
  let mediaReserve = 0;
  const visible = (node) => node && !node.hidden && !node.classList.contains('is-hidden') && !node.classList.contains('is-fullscreen');
  function measureMedia() {
    const shown = [doc.querySelector?.('.kira-video-dock'), doc.querySelector?.('.kira-now-playing')].filter(visible);
    const height = shown.reduce((most, node) => Math.max(most, node.offsetHeight || 0), 0);
    const next = height ? height + 8 : 0;
    if (next === mediaReserve) return;
    mediaReserve = next;
    body.style?.setProperty?.('--phone-media-h', `${next}px`);
    body.classList.toggle('phone-has-media', next > 0);
    win.dispatchEvent?.(new win.CustomEvent('kira:phone-layout', { detail: { mediaReserve: next } }));
  }
  const soon = () => { win.setTimeout?.(measureMedia, 30); win.setTimeout?.(measureMedia, 400); };
  win.addEventListener?.('kira:video-dock', soon);
  win.addEventListener?.('kira:music-state', soon);

  setMode(mode);
  subscribe(false).catch(() => {}); // já autorizado antes: renova a inscrição (o PC pode ter apagado a antiga)
  return { setMode, setTranscript, toggleMenu, enableNotifications, measureMedia, get mode() { return mode; }, get mediaReserve() { return mediaReserve; } };
}

if (globalThis.window?.KIRA_REMOTE && globalThis.document?.body && !globalThis.window.KiraRemoteShell) {
  globalThis.window.KiraRemoteShell = createRemoteShell();
}
