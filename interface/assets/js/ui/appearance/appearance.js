// Aparência: fundo (galáxia, preto, preto com grade), estilo do orbe (galáxia do kira-3d.js, círculo, partículas), cor
// e o visual do mini player da música no canto (nowPlaying.js).
// A escolha fica no localStorage do app; a galáxia continua sendo o kira-3d.js, só pausada enquanto outro orbe aparece.
import { ACCENTS, ORB_RENDERERS } from './orbRenderers.js';
import { createThinkingRenderer } from './thinkingOrbs.js';
import './tvFeed.js'; // orbe da TV acompanha a voz da Kira (só com a TV viva)

ORB_RENDERERS.thinking = createThinkingRenderer; // Thinking Orbs: 9 estados reais, mesmo desenho dos mini-orbes

const STORAGE_KEY = 'KIRA_APPEARANCE';
export const ORB_STYLES = [
  ['nuvem', 'Nuvem de Partículas'], ['colmeia', 'Colmeia Geodésica'], ['voxels', 'Voxels'],
  ['globo', 'Globo 3D'],
  ['galaxy', 'Galáxia'], ['ring', 'Círculo'], ['particles', 'Partículas'],
  ['constellation', 'Constelação'], ['hive', 'Colmeia'], ['blackhole', 'Buraco negro'],
  ['datarings', 'Anéis de dados'], ['plasma', 'Núcleo de plasma'], ['torus', 'Toro de luz'], ['ribbons', 'Fitas quânticas'],
  ['fusion', 'Os quatro juntos'], ['thinking', 'Thinking Orbs'],
];
export const BACKGROUNDS = [['galaxy', 'Galáxia'], ['black', 'Preto'], ['grid', 'Grade']];
export const ACCENT_NAMES = [['roxo', 'Roxo'], ['ciano', 'Ciano'], ['rosa', 'Rosa'], ['ambar', 'Âmbar']];
export const PLAYER_STYLES = [['neon', 'Neon'], ['capsula', 'Cápsula'], ['vinil', 'Vinil'], ['onda', 'Onda']];
export const MENU_STYLES = [['trilho', 'Líquido (antigo)'], ['padrao', 'Atual']];
export const CHAT_STYLES = [['padrao', 'Padrão'], ['orbe', 'Sobre o orbe']];
export const HOME_STYLES = [['cartao', 'Cartão de vidro'], ['lista', 'Lista compacta']]; // o controle remoto fica sempre ao lado
export const CLOCK_STYLES = [['padrao', 'Padrão'], ['minimo', 'Mínimo'], ['neon', 'Neon'], ['vidro', 'Vidro'], ['terminal', 'Terminal'], ['analogico', 'Analógico'], ['oculto', 'Sem relógio']];
export const DEFAULTS = { orb: 'nuvem', bg: 'galaxy', accent: 'roxo', player: 'neon', menu: 'trilho', chat: 'padrao', clock: 'padrao', home: 'cartao' };
const STATES = new Set(['idle', 'listening', 'thinking', 'speaking', 'error']);
// Falando/ouvindo pede quadro fluido; parado, o orbe se mexe devagar e 20 quadros bastam.
// 2026-11-01: nesta máquina (4 núcleos, placa na lista de bloqueio do Chromium) cada quadro custa CPU no processo da
// janela e a voz da Kira atrasava; 40 falando e 20 parado deixam a CPU para a conversa.
// Quadros por segundo (Aparência > Animações): 'seguro' = 40 falando/20 parado. A GTX 550 Ti (driver 2017) derruba a
// Kira com 100 (Qt6WebEngineCore +0xfc51b3 ao trocar de orbe, 2026-11-02); 60 e 100 ficam para outro monitor/placa.
export const fpsChoice = () => { try { return globalThis.localStorage?.getItem('kira.fps') || 'seguro'; } catch (_) { return 'seguro'; } };
const frameMs = (active) => { const c = fpsChoice(); return c === 'seguro' ? 1000 / (active ? 40 : 20) : 1000 / Number(c); };
// Nível do analisador da voz da Kira (index.html) numa fala normal fica entre 0,15 e 0,30: 0,3 já é "voz cheia".
const VOICE_FULL_SCALE = 0.3;

function pick(value, options, fallback) {
  return options.some(([key]) => key === value) ? value : fallback;
}

/** Preferência salva -> valores válidos (o que vier estragado volta ao padrão). */
export function parsePrefs(raw) {
  let data = {};
  try { data = JSON.parse(raw || '{}') || {}; } catch (_) { data = {}; }
  return {
    orb: pick(data.orb, ORB_STYLES, DEFAULTS.orb),
    bg: pick(data.bg, BACKGROUNDS, DEFAULTS.bg),
    accent: pick(data.accent, ACCENT_NAMES, DEFAULTS.accent),
    player: pick(data.player, PLAYER_STYLES, DEFAULTS.player),
    menu: pick(data.menu, MENU_STYLES, DEFAULTS.menu),
    chat: pick(data.chat, CHAT_STYLES, DEFAULTS.chat),
    clock: pick(data.clock, CLOCK_STYLES, DEFAULTS.clock),
    home: pick(data.home, HOME_STYLES, DEFAULTS.home),
  };
}

/** Estado do orbe: a classe da .galaxy-system já respeita o botão "Animação de fala". */
export function orbState(documentRef, windowRef) {
  const system = documentRef.querySelector('.galaxy-system');
  const found = system ? [...system.classList].find((name) => STATES.has(name)) : null;
  if (windowRef._kiraOrbVoiceAnimEnabled === false) return 'idle';
  return found || 'idle';
}

export function createAppearance({ documentRef = globalThis.document, windowRef = globalThis.window, storage = globalThis.localStorage } = {}) {
  const root = documentRef.documentElement;
  let prefs = parsePrefs(read());
  let canvas = null;
  let renderer = null;
  let rendererName = '';
  let frameId = 0;
  let idleTimer = 0;
  let lastFrame = 0;
  let level = 0;
  let paused = read('KIRA_ORB_PAUSED') === 'true';
  let resizeObserver = null;

  function read(key = STORAGE_KEY) {
    try { return storage?.getItem(key) ?? null; } catch (_) { return null; }
  }

  function galaxy() {
    return windowRef.kira3D || windowRef.__kira3dInstance || null;
  }

  // Outro orbe na tela: a galáxia fica parada por este motivo (o botão "Pausar" é outro motivo, em kira-3d.js).
  function holdGalaxy() {
    galaxy()?.hold?.('appearance', true);
  }

  function releaseGalaxy() {
    galaxy()?.hold?.('appearance', false);
  }

  function ensureCanvas() {
    if (canvas?.isConnected) return canvas;
    const wrapper = documentRef.querySelector('.orb-wrapper');
    if (!wrapper) return null;
    canvas = documentRef.createElement('canvas');
    canvas.className = 'orb-alt-canvas';
    canvas.setAttribute('aria-hidden', 'true');
    wrapper.appendChild(canvas);
    resizeObserver?.disconnect();
    if (typeof windowRef.ResizeObserver === 'function') {
      resizeObserver = new windowRef.ResizeObserver(() => fit());
      resizeObserver.observe(wrapper);
    }
    fit();
    return canvas;
  }

  function fit() {
    if (!canvas) return;
    const box = canvas.parentElement?.getBoundingClientRect?.() || { width: 0, height: 0 };
    const scale = Math.min(windowRef.devicePixelRatio || 1, 1); // 1.5x custava o dobro de pixels nos orbes em 2D
    canvas.width = Math.max(1, Math.round(box.width * scale));
    canvas.height = Math.max(1, Math.round(box.height * scale));
  }

  function tick(now) {
    frameId = 0;
    // 'globo' é WebGL 3D (orb3d.js), 'galaxy' é WebGL (kira-3d.js): o canvas 2D não desenha nesses modos
    if (prefs.orb === 'galaxy' || prefs.orb === 'globo' || paused || documentRef.hidden) return;
    holdGalaxy(); // a galáxia pode ter nascido depois (só inicia na tela principal)
    const working = Boolean(windowRef.kiraActivity && windowRef.kiraActivity.until > Date.now());
    let state = orbState(documentRef, windowRef);
    // ferramenta longa rodando (PDF, busca, TV...) e ninguém falando: o orbe trabalha em vez de ficar parado
    if (working && (state === 'idle' || state === 'listening')) state = 'thinking';
    windowRef.kiraVoiceState = state; // mini-orbes <kira-orb> seguem o mesmo estado
    windowRef.kiraAccent = ACCENTS[prefs.accent];
    const active = state !== 'idle' || working || Boolean(windowRef.kiraOrbView?.moving); // girando com o mouse: quadro cheio
    if (now - lastFrame >= frameMs(active) - 1) {
      lastFrame = now;
      const target = state === 'speaking' ? Math.max(0, Math.min(1, (Number(windowRef.kiraVoiceLevel) || 0) / VOICE_FULL_SCALE)) : 0;
      level += (target - level) * (target > level ? 0.35 : 0.08);
      const surface = ensureCanvas();
      const ctx = surface?.width > 1 ? surface.getContext('2d') : null;
      if (ctx) renderer.draw(ctx, { t: now / 1000, w: surface.width, h: surface.height, state, level, accent: ACCENTS[prefs.accent] });
    }
    // Parado, o próximo quadro vem por timer: pedir quadro a cada refresh fazia a página montar 60 por segundo.
    schedule(active || level > 0.01 ? 0 : frameMs(false));
  }

  function cancel() {
    if (frameId) windowRef.cancelAnimationFrame(frameId);
    if (idleTimer) windowRef.clearTimeout(idleTimer);
    frameId = 0;
    idleTimer = 0;
  }

  function schedule(delay = 0) {
    if (frameId || idleTimer || prefs.orb === 'galaxy' || paused || documentRef.hidden) return;
    if (delay <= 0) {
      frameId = windowRef.requestAnimationFrame(tick);
      return;
    }
    idleTimer = windowRef.setTimeout(() => {
      idleTimer = 0;
      schedule();
    }, delay);
  }

  function apply() {
    // Trocou de orbe: só um esmaecer curto (o giro/implosão 3D foi tirado a pedido do Chefe, 2026-11-05).
    if (root.dataset.kiraOrb && root.dataset.kiraOrb !== prefs.orb) {
      documentRef.querySelector('.orb-wrapper')?.animate?.([{ opacity: 0 }, { opacity: 1 }], { duration: 350, easing: 'ease-out' });
    }
    root.dataset.kiraBg = prefs.bg;
    root.dataset.kiraOrb = prefs.orb;
    root.dataset.kiraAccent = prefs.accent;
    windowRef.kiraAppearancePrefs = prefs; // orb3d.js lê a cor de acento daqui
    root.dataset.kiraPlayer = prefs.player;
    root.dataset.kiraMenu = prefs.menu;
    root.dataset.kiraChat = prefs.chat;
    root.dataset.kiraClock = prefs.clock;
    root.dataset.kiraHome = prefs.home; // Casa inteligente: cartão, lista ou controle remoto (só CSS)
    if (prefs.clock === 'analogico') ensureDial(documentRef, windowRef);
    decorateSidebar(documentRef);
    root.style.setProperty('--kira-accent-rgb', ACCENTS[prefs.accent].main.join(', '));
    if (prefs.orb === 'galaxy') {
      cancel();
      releaseGalaxy();
      return;
    }
    if (!ORB_RENDERERS[prefs.orb]) { // 'globo' é WebGL (orb3d.js): sem desenho 2D. Antes quebrava a aba Aparência inteira.
      cancel();
      holdGalaxy();
      return;
    }
    if (rendererName !== prefs.orb) {
      renderer = ORB_RENDERERS[prefs.orb]();
      rendererName = prefs.orb;
    }
    holdGalaxy();
    ensureCanvas();
    schedule();
  }

  function set(patch) {
    prefs = parsePrefs(JSON.stringify({ ...prefs, ...patch }));
    try { storage?.setItem(STORAGE_KEY, JSON.stringify(prefs)); } catch (_) { /* sem storage: vale até fechar */ }
    apply();
    return prefs;
  }

  const onVisibility = () => schedule();
  documentRef.addEventListener('visibilitychange', onVisibility);
  apply();

  return {
    get prefs() { return { ...prefs }; },
    set,
    /** Botão "Pausar animação" da aba Aparência: vale para todos os orbes. */
    setPaused(value) {
      paused = Boolean(value);
      if (paused) cancel();
      schedule();
    },
    resume: schedule,
    destroy() {
      cancel();
      resizeObserver?.disconnect();
      documentRef.removeEventListener('visibilitychange', onVisibility);
      canvas?.remove();
      releaseGalaxy();
    },
  };
}

/** Relógio analógico: um mostrador em SVG dentro do #header-clock, ponteiros atualizados a cada segundo. */
function ensureDial(documentRef, windowRef) {
  const clock = documentRef?.getElementById?.('header-clock');
  if (!clock || clock.querySelector('.kira-clock-dial')) return;
  const ns = 'http://www.w3.org/2000/svg';
  const svg = documentRef.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 100 100');
  svg.setAttribute('class', 'kira-clock-dial');
  let marks = '';
  for (let i = 0; i < 12; i += 1) {
    const a = (i / 12) * Math.PI * 2;
    const big = i % 3 === 0;
    marks += `<line x1="${50 + Math.cos(a) * 40}" y1="${50 + Math.sin(a) * 40}" x2="${50 + Math.cos(a) * 45}" y2="${50 + Math.sin(a) * 45}" stroke="rgba(240,171,252,${big ? 0.9 : 0.45})" stroke-width="${big ? 2 : 1}"/>`;
  }
  svg.innerHTML = `${marks}<line class="h" x1="50" y1="50" x2="50" y2="28" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/><line class="m" x1="50" y1="50" x2="50" y2="16" stroke="#e9d5ff" stroke-width="2" stroke-linecap="round"/><line class="s" x1="50" y1="56" x2="50" y2="10" stroke="#e879f9" stroke-width="1"/><circle cx="50" cy="50" r="2.6" fill="#e879f9"/>`;
  clock.appendChild(svg);
  const turn = (sel, deg) => svg.querySelector(sel).setAttribute('transform', `rotate(${deg} 50 50)`);
  const tick = () => {
    const d = new Date();
    turn('.h', ((d.getHours() % 12) + d.getMinutes() / 60) * 30);
    turn('.m', d.getMinutes() * 6);
    turn('.s', d.getSeconds() * 6);
  };
  tick();
  windowRef?.setInterval?.(tick, 1000);
}

/**
 * Menu lateral (estilos da aba Aparência): cor de cada ícone (estilo "apps"), item ativo marcado com .kui-on e o
 * bloco do estilo "trilho" que escorrega até ele. O Sidebar.js monta o menu depois: confere de novo quando ele nasce.
 */
export function decorateSidebar(documentRef = globalThis.document) {
  const rail = documentRef?.querySelector?.('.kui-sidebar');
  if (!rail || typeof rail.querySelectorAll !== 'function') {
    if (!rail && documentRef?.body && !documentRef.__kiraMenuWatch && typeof MutationObserver === 'function') {
      documentRef.__kiraMenuWatch = new MutationObserver(() => {
        if (!documentRef.querySelector('.kui-sidebar')) return;
        documentRef.__kiraMenuWatch.disconnect();
        decorateSidebar(documentRef);
      });
      documentRef.__kiraMenuWatch.observe(documentRef.body, { childList: true });
    }
    return;
  }
  const actions = [...rail.querySelectorAll('.kui-sidebar-action')];
  actions.forEach((button, index) => button.classList.add(`kui-c${index % 5}`));
  let blob = rail.querySelector('.kui-menu-blob');
  if (!blob) {
    blob = documentRef.createElement('div');
    blob.className = 'kui-menu-blob';
    rail.prepend(blob);
  }
  // Posição pelo layout (offsetTop somado até o trilho), não pelo retângulo na tela: durante a animação de abrir/fechar
  // o menu os títulos encolhem e o retângulo mentia (o bloco ficava ~20 px abaixo do ícone).
  const layoutTop = (el) => { let y = 0; for (let n = el; n && n !== rail; n = n.offsetParent) y += n.offsetTop; return y; };
  const place = () => {
    const on = rail.querySelector('.kui-sidebar-action.kui-on');
    const height = on?.offsetHeight || 0;
    blob.style.opacity = height ? '1' : '0';
    if (!height) return;
    blob.style.transform = `translate3d(0, ${layoutTop(on)}px, 0)`;
    blob.style.height = `${height}px`;
  };
  if (!rail.__kiraMenuBound) {
    rail.__kiraMenuBound = true;
    rail.addEventListener('click', (event) => {
      const hit = event.target.closest?.('.kui-sidebar-action');
      if (!hit || hit.disabled) return;
      actions.forEach((button) => button.classList.toggle('kui-on', button === hit));
      place();
    });
    // qualquer transição do menu (largura, títulos encolhendo) muda onde o ícone fica; só o próprio bloco não conta
    rail.addEventListener('transitionend', (event) => { if (event.target !== blob) place(); });
    if (typeof ResizeObserver === 'function') new ResizeObserver(place).observe(rail);
    // medido no meio de uma troca de tela (Estudos abrindo, menu expandindo) o bloco ficava fora do ícone: remede
    // depois que o layout assenta
    const view = documentRef.defaultView || globalThis;
    const remeasure = () => view.requestAnimationFrame(() => view.requestAnimationFrame(place));
    view.addEventListener('resize', remeasure);
    if (typeof MutationObserver === 'function') new MutationObserver(remeasure).observe(documentRef.body, { attributes: true, attributeFilter: ['class'] });
    // Painel do menu (casa, lembretes...) fechado: a marca volta para a tela em que você está (Principal ou Chat).
    // A marca segue a tela de verdade: Estudos aberto acende Estudos; senão Chat ou Principal (painel do menu aberto
    // por clique mantém a marca nele até fechar).
    const settle = () => {
      const body = documentRef.body;
      let target = null;
      if (body.classList.contains('kira-studies-open')) target = rail.querySelector('[data-kui-assistant="tutor"]');
      else {
        const on = rail.querySelector('.kui-sidebar-action.kui-on');
        if (on && !on.dataset.kuiScreen && on.dataset.kuiAssistant !== 'tutor' && documentRef.querySelector('.kui-window:not([hidden])')) return;
        target = rail.querySelector(`[data-kui-screen="${body.classList.contains('kui-chat-open') ? 'chat' : 'main'}"]`);
      }
      if (!target || target.classList.contains('kui-on')) { place(); return; }
      actions.forEach((button) => button.classList.toggle('kui-on', button === target));
      place();
    };
    if (typeof MutationObserver === 'function') new MutationObserver(() => view.requestAnimationFrame(settle)).observe(documentRef.body, { attributes: true, attributeFilter: ['class'] });
    documentRef.addEventListener('click', () => setTimeout(settle, 120), true);
    documentRef.addEventListener('keydown', () => setTimeout(settle, 120), true);
    setInterval(settle, 2000); // fechado por voz não passa por clique
  }
  if (!rail.querySelector('.kui-on')) rail.querySelector('[data-kui-screen="main"]')?.classList.add('kui-on');
  place();
}

function option(documentRef, group, value, label, selected, onPick, decorate) {
  const button = documentRef.createElement('button');
  button.type = 'button';
  button.className = `appearance-option${selected ? ' is-selected' : ''}`;
  button.dataset.value = value;
  button.setAttribute('role', 'radio');
  button.setAttribute('aria-checked', selected ? 'true' : 'false');
  decorate?.(button);
  const text = documentRef.createElement('span');
  text.className = 'appearance-option-label';
  text.textContent = label;
  button.appendChild(text);
  button.addEventListener('click', () => {
    for (const other of group.querySelectorAll('.appearance-option')) {
      const on = other === button;
      other.classList.toggle('is-selected', on);
      other.setAttribute('aria-checked', on ? 'true' : 'false');
    }
    onPick(value);
  });
  return button;
}

/** Miniatura parada de menu/chat/relógio: peças vazias que o CSS (appearance.css, .appearance-mini-*) desenha. */
function mini(key, parts) {
  return (button, value) => {
    const box = button.ownerDocument.createElement('span');
    box.className = `appearance-option-preview appearance-mini appearance-mini-${key} is-${value}`;
    for (const part of parts) box.appendChild(button.ownerDocument.createElement('i')).className = part;
    button.appendChild(box);
  };
}

function preview(documentRef, name, accent) {
  const canvas = documentRef.createElement('canvas');
  canvas.className = 'appearance-option-preview';
  canvas.width = 96;
  canvas.height = 96;
  const ctx = canvas.getContext?.('2d');
  if (ctx && ORB_RENDERERS[name]) {
    ORB_RENDERERS[name]({ count: 180, orbiters: 14 }).draw(ctx, { t: 2.4, w: 96, h: 96, state: 'speaking', level: 0.55, accent: ACCENTS[accent] });
  }
  return canvas;
}

/** Monta os seletores da aba Aparência, cada um no seu cartão ([data-appearance-group="orb" | "bg" | "accent"]). */
export function mountAppearanceSection(panel, appearance, documentRef = globalThis.document) {
  if (!panel || panel.querySelector('.appearance-options')) return null;
  const groups = [
    ['orb', 'Orbe', ORB_STYLES, (button, value) => {
      if (value === 'galaxy' || value === 'globo') { // globo é WebGL: arte própria em CSS (antes ficava um círculo preto)
        const art = documentRef.createElement('span');
        art.className = `appearance-option-preview ${value === 'globo' ? 'appearance-globe-art' : 'appearance-galaxy-art'}`;
        button.appendChild(art);
      } else {
        button.appendChild(preview(documentRef, value, appearance.prefs.accent));
      }
    }],
    ['bg', 'Fundo', BACKGROUNDS, (button, value) => {
      const swatch = documentRef.createElement('span');
      swatch.className = `appearance-option-preview appearance-bg-${value}`;
      button.appendChild(swatch);
    }],
    ['accent', 'Cor do orbe e da grade', ACCENT_NAMES, (button, value) => {
      const dot = documentRef.createElement('span');
      dot.className = 'appearance-accent-dot';
      dot.style.setProperty('--dot', `rgb(${ACCENTS[value].main.join(',')})`);
      button.appendChild(dot);
    }],
    ['player', 'Mini player da música', PLAYER_STYLES, (button, value) => {
      // Miniatura parada do cartão: capa, duas linhas de texto e o equalizador (o "onda" mostra as barras).
      const mock = documentRef.createElement('span');
      mock.className = `appearance-option-preview appearance-player appearance-player-${value}`;
      for (const part of ['art', 'lines', 'eq']) {
        const piece = documentRef.createElement('span');
        piece.className = `appearance-player-${part}`;
        mock.appendChild(piece);
      }
      button.appendChild(mock);
    }],
    ['menu', 'Menu lateral', MENU_STYLES, mini('menu', ['i', 'i', 'i', 'i'])],
    ['chat', 'Chat', CHAT_STYLES, mini('chat', ['o', 'b', 'b'])],
    ['clock', 'Relógio', CLOCK_STYLES, mini('clock', ['t'])],
    ['home', 'Casa inteligente', HOME_STYLES, mini('home', ['a', 'b', 'c'])],
  ];
  const mounted = [];
  for (const [key, title, options, decorate] of groups) {
    const host = panel.querySelector(`[data-appearance-group="${key}"]`);
    if (!host) continue;
    const group = documentRef.createElement('div');
    group.className = `appearance-options appearance-options-${key}`;
    group.setAttribute('role', 'radiogroup');
    group.setAttribute('aria-label', title);
    for (const [value, label] of options) {
      group.appendChild(option(documentRef, group, value, label, appearance.prefs[key] === value, (picked) => {
        appearance.set({ [key]: picked });
        if (key === 'accent') {
          // As miniaturas acompanham a cor nova.
          for (const old of panel.querySelectorAll('.appearance-options-orb canvas')) {
            old.replaceWith(preview(documentRef, old.parentElement.dataset.value, picked));
          }
        }
      }, (button) => decorate?.(button, value)));
    }
    host.appendChild(group);
    mounted.push(group);
  }
  return mounted;
}

if (globalThis.document?.documentElement && globalThis.window && !globalThis.window.KiraAppearance) {
  const appearance = createAppearance();
  globalThis.window.KiraAppearance = appearance;
  const mount = () => mountAppearanceSection(globalThis.document.querySelector('#config-panel-visual'), appearance);
  if (globalThis.document.readyState === 'loading') globalThis.document.addEventListener('DOMContentLoaded', mount, { once: true });
  else mount();
}
