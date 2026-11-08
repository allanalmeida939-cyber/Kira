// Aba Internet: o navegador da Kira ao vivo. A imagem vem do backend em MJPEG (/api/browser/live) só com a aba aberta
// e a janela à mostra; clique, rolagem e teclas na imagem vão para a página; barra com voltar, avançar, recarregar e
// endereço; a linha de status mostra o que a Kira está fazendo e, quando ela pergunta algo, um campo para responder
// escrevendo (senha não precisa ser falada). Tudo por textContent: títulos e textos vêm de sites.

const STATE_MS = 1500;
const WHEEL_MS = 90;
const RECONNECT_MS = 5000;
const SPECIAL_KEYS = new Set(['Enter', 'Tab', 'Escape', 'Backspace', 'Delete', 'Home', 'End', 'PageUp', 'PageDown',
  'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight']);

/** Ponto clicado na imagem -> posição de 0 a 1 na página, descontando as faixas do object-fit: contain. null fora. */
export function mapPoint(rect, natural, clientX, clientY) {
  const width = Number(rect?.width) || 0;
  const height = Number(rect?.height) || 0;
  const naturalWidth = Number(natural?.width) || 0;
  const naturalHeight = Number(natural?.height) || 0;
  if (!(width > 0 && height > 0 && naturalWidth > 0 && naturalHeight > 0)) return null;
  const scale = Math.min(width / naturalWidth, height / naturalHeight);
  const shownWidth = naturalWidth * scale;
  const shownHeight = naturalHeight * scale;
  const x = (Number(clientX) - (Number(rect.left) || 0) - (width - shownWidth) / 2) / shownWidth;
  const y = (Number(clientY) - (Number(rect.top) || 0) - (height - shownHeight) / 2) / shownHeight;
  if (!(x >= 0 && x <= 1 && y >= 0 && y <= 1)) return null;
  return { x: Math.round(x * 10000) / 10000, y: Math.round(y * 10000) / 10000 };
}

/** Tecla na imagem -> o que mandar: tecla especial, uma letra, ou nada (atalhos com Ctrl/Alt ficam no Windows). */
export function keyInput(event) {
  if (!event || event.ctrlKey || event.metaKey || event.altKey) return null;
  const key = String(event.key || '');
  if (SPECIAL_KEYS.has(key)) return { type: 'key', key: event.shiftKey && key === 'Tab' ? 'Shift+Tab' : key };
  if ([...key].length === 1) return { type: 'type', text: key };
  return null;
}

const PAGE_SCALE = 1.25;

/** Linha de status da tarefa: o que a Kira está fazendo, a pergunta dela ou o resultado. */
export function statusLine(state = {}) {
  const task = state.task || {};
  // a pergunta é feita por voz (o Chefe não quer caixinha para escrever); aqui só o aviso de que ela está esperando
  if (task.state === 'waiting' && task.question) return { text: 'Kira: esperando sua resposta por voz', kind: 'waiting' };
  if (task.state === 'running') return { text: `Kira: ${task.note || 'trabalhando nisso…'}`, kind: 'running' };
  if (task.state === 'done' && task.result) return { text: `Pronto: ${task.result}`, kind: 'done' };
  if (task.state === 'failed' && task.result) return { text: `Parou: ${task.result}`, kind: 'failed' };
  return { text: '', kind: 'idle' };
}

export function createLiveBrowser({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  const doc = documentRef;
  const $ = (id) => doc.getElementById(id);
  const drawer = $('browser-drawer');
  const view = $('kira-browser-view');
  const stage = $('brw-stage');
  const address = $('brw-address');
  const answer = $('brw-answer');
  let timer = 0;
  let watching = false;
  let lastReconnect = 0;
  let pendingWheel = null;

  const post = (path, body = {}) => windowRef.fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), cache: 'no-store',
  }).then((response) => response.json()).catch(() => null);
  const say = (id, text) => { const node = $(id); if (node) node.textContent = text; };
  const show = (id, visible) => { const node = $(id); if (node) node.hidden = !visible; };

  function connect() {
    lastReconnect = Date.now();
    if (view) view.src = `/api/browser/live?t=${lastReconnect}`;
  }

  function render(state = {}) {
    const open = Boolean(state.open);
    show('kira-browser-placeholder', !open);
    if (view) view.hidden = !open;
    say('brw-page-title', state.title || (open ? state.url || '' : 'Navegador da Kira'));
    if (address && doc.activeElement !== address) address.value = open ? String(state.url || '') : '';
    for (const id of ['brw-back', 'brw-forward', 'brw-reload']) { const button = $(id); if (button) button.disabled = !open; }
    const line = statusLine(state);
    show('brw-status', Boolean(line.text));
    say('brw-status-text', line.text);
    const status = $('brw-status');
    if (status) status.dataset.kind = line.kind;
    show('brw-answer-form', false);
    const secret = Boolean(state.task?.secret) || (line.kind === 'waiting' && /senha/i.test(String(state.task?.question || '')));
    if (answer) answer.type = secret ? 'password' : 'text';
    // o chat principal também manda a senha digitada direto para a tarefa, sem mostrar
    windowRef.KiraBrowserSecretExpected = secret;
    show('brw-stop-task', Boolean(state.task?.active));
    // A imagem caiu (o backend reiniciou): esta aba aberta não está sendo contada como quem vê. Reconecta.
    if (watching && open && !(Number(state.viewers) > 0) && Date.now() - lastReconnect > RECONNECT_MS) connect();
  }

  async function refresh() {
    try {
      const response = await windowRef.fetch('/api/browser/state', { cache: 'no-store' });
      const payload = await response.json();
      if (payload?.success) render(payload.data || {});
    } catch (_) {
      /* aba continua com o último estado */
    }
  }

  // a página do navegador ganha o tamanho exato desta área: imagem 1:1, sem encolher (encolhida o texto borrava)
  let sizeTimer = 0;
  function fitPage() {
    // 1:1 ficava nítido mas grande demais (site "ampliado"): a página tem 25% a mais e a imagem encolhe um pouco
    const width = Math.round((stage?.clientWidth || 0) * PAGE_SCALE);
    const height = Math.round((stage?.clientHeight || 0) * PAGE_SCALE);
    if (!watching || width < 200 || height < 200) return;
    windowRef.clearTimeout?.(sizeTimer);
    sizeTimer = windowRef.setTimeout?.(() => post('/api/browser/control', { action: 'resize', width, height }).catch(() => {}), 250);
  }
  if (typeof windowRef.ResizeObserver === 'function' && stage) new windowRef.ResizeObserver(fitPage).observe(stage);

  function start() {
    if (watching) return;
    watching = true;
    fitPage();
    connect();
    refresh();
    timer = windowRef.setInterval(refresh, STATE_MS);
  }

  function stop() {
    if (!watching) return;
    watching = false;
    view?.removeAttribute?.('src'); // fecha a conexão da imagem: o backend para a captura
    windowRef.clearInterval(timer);
    timer = 0;
  }

  let warmed = false;
  const sync = () => {
    const open = drawer?.classList?.contains('open') && !doc.hidden;
    if (open && !warmed) { warmed = true; post('/api/browser/start').then(refresh); } // liga já: pronto quando ele pedir
    return open ? start() : stop();
  };

  function point(event) {
    if (!view) return null;
    return mapPoint(view.getBoundingClientRect(), { width: view.naturalWidth, height: view.naturalHeight }, event.clientX, event.clientY);
  }

  view?.addEventListener('click', (event) => {
    const where = point(event);
    stage?.focus?.();
    if (where) post('/api/browser/input', { type: 'click', ...where });
  });
  stage?.addEventListener('wheel', (event) => {
    const where = point(event) || { x: 0.5, y: 0.5 };
    event.preventDefault?.();
    if (pendingWheel) {
      pendingWheel.dy += Number(event.deltaY) || 0;
      return;
    }
    pendingWheel = { ...where, dy: Number(event.deltaY) || 0 };
    windowRef.setTimeout(() => {
      const wheel = pendingWheel;
      pendingWheel = null;
      if (wheel?.dy) post('/api/browser/input', { type: 'wheel', ...wheel });
    }, WHEEL_MS);
  }, { passive: false });
  // Digitação: uma tecla por pedido atrasava segundos (e podia trocar a ordem); as letras seguidas vão juntas, em fila.
  const keyQueue = [];
  let keySending = false;
  function queueKey(input) {
    const last = keyQueue[keyQueue.length - 1];
    if (input.type === 'type' && last?.type === 'type') last.text += input.text;
    else keyQueue.push({ ...input });
    if (!keySending) flushKeys();
  }
  async function flushKeys() {
    keySending = true;
    while (keyQueue.length) await post('/api/browser/input', keyQueue.shift());
    keySending = false;
  }
  stage?.addEventListener('keydown', (event) => {
    const input = keyInput(event);
    if (!input) return;
    event.preventDefault?.();
    if (input.key === 'Escape') event.stopPropagation?.(); // Esc na página vai para o site, não fecha a aba
    queueKey(input);
  });
  // Atalhos (aba vazia): um toque abre o site no navegador da Kira
  doc.querySelectorAll?.('.brw-dial [data-url]').forEach((tile) => tile.addEventListener('click', () => {
    say('brw-page-title', 'Abrindo…');
    post('/api/browser/navigate', { url: tile.dataset.url }).then(refresh);
  }));
  $('brw-address-form')?.addEventListener('submit', (event) => {
    event?.preventDefault?.();
    const target = String(address?.value || '').trim();
    if (!target) return;
    address?.blur?.();
    say('brw-page-title', 'Abrindo…');
    post('/api/browser/navigate', { url: target }).then(refresh);
  });
  for (const [id, action] of [['brw-back', 'back'], ['brw-forward', 'forward'], ['brw-reload', 'reload']]) {
    $(id)?.addEventListener('click', () => post('/api/browser/control', { action }).then(refresh));
  }
  $('brw-stop-task')?.addEventListener('click', () => post('/api/browser/task/stop').then(refresh));
  $('brw-answer-form')?.addEventListener('submit', (event) => {
    event?.preventDefault?.();
    const reply = String(answer?.value || '').trim();
    if (!reply) return;
    if (answer) answer.value = '';
    post('/api/browser/task/answer', { answer: reply }).then(refresh);
  });

  if (drawer && windowRef.MutationObserver) new windowRef.MutationObserver(sync).observe(drawer, { attributes: true, attributeFilter: ['class'] });
  doc.addEventListener?.('visibilitychange', sync);
  sync();
  return { refresh, render, start, stop };
}

if (globalThis.document?.getElementById?.('browser-drawer') && globalThis.window && !globalThis.window.KiraLiveBrowser) {
  globalThis.window.KiraLiveBrowser = createLiveBrowser();
}
