// Olho de Deus: a interface da Kira some e um globo ocupa a tela, com terremotos (USGS), aviões (adsb.lol) e satélites
// (CelesTrak + SGP4). Leve de propósito: three.js com textura plana e pontos (o PC renderiza WebGL sem GPU).
// Aproximando além do limite do globo, ele vira um mapa de satélite até as ruas (godsEyeMap.js).
// Controles: arrastar gira, roda aproxima, 1-4 liga camadas, setas giram, Esc sai. Por voz: tool olho_de_deus.

import { createGodsEye3d } from './godsEye3d.js';
import { createGodsEyeMap, ENTER_ZOOM } from './godsEyeMap.js';

const D2R = Math.PI / 180;
const SATELLITE_JS = 'https://cdn.jsdelivr.net/npm/satellite.js@5.0.0/dist/satellite.min.js';
const EARTH_TEXTURE = 'assets/ui/earth-night.jpg';
const frameMsNow = () => { let c = 'seguro'; try { c = localStorage.getItem('kira.fps') || 'seguro'; } catch (_) {} return 1000 / (c === 'seguro' ? 30 : Number(c)); };
// arrastando com o dedo/mouse: todo quadro (60 fps), senão o giro dava trancos; parado volta a 30
const INPUT_SMOOTH_MS = 1200;
const PLANES_EVERY_MS = 12000;
const QUAKES_EVERY_MS = 5 * 60000;
const IDLE_SPIN_AFTER_MS = 6000;
const DEFAULT_FOCUS = { lat: -3.72, lon: -38.54, label: 'Fortaleza' };
// Fotos do cartão do lugar: só destes servidores (satélite da Esri, Wikimedia Commons, Mapillary).
const PLACE_PHOTO_HOSTS = ['https://server.arcgisonline.com/', 'https://upload.wikimedia.org/', 'https://scontent'];
const GLOBE_REVEAL_MS = 700;
const LIGHTNING_EVERY_MS = 15000;
// Visual (godseye-themes.css): o modelo escolhido pelo Chefe é o padrão; a prévia troca com setTheme
export const THEMES = ['nebulosa', 'artico', 'aurora', 'solar'];
const DEFAULT_THEME = 'nebulosa';
const THEME_KEY = 'kira.godseye.theme';
const BOOT_MS = 3200; // ativação longa para dar tempo de ver (anéis, varredura, painéis entrando) // animação de ativação: anéis, varredura, painéis entrando um a um
const BOOT_CAMERA_Z = 9; // o globo chega de longe até a distância normal
const SHIPS_EVERY_MS = 30000;
const RAIN_EVERY_MS = 10 * 60000;
const RAIN_GLOBE_ZOOM = 2; // 4x4 quadros do radar viram a textura da esfera de chuva
// queimadas, tempestades, vulcões... (NASA EONET): cor por tipo, o resto num grupo só
const EVENT_COLORS = { wildfires: 0xff5a1f, severeStorms: 0x38bdf8, volcanoes: 0xef4444, seaLakeIce: 0xe0f2fe, outros: 0x4ade80 };
const APOD_IMAGE_RE = /^https:\/\/(apod\.nasa\.gov|assets\.science\.nasa\.gov)\//;

/** Quadro z/x/y do radar de chuva (RainViewer). */
export function rainTileUrl(rain, z, x, y) {
  return `${rain.host}${rain.path}/256/${z}/${x}/${y}/2/1_1.png`;
}

/** Latitude -> linha (0..1) na projeção dos mapas da web (Mercator), para reprojetar o radar na esfera. */
export function mercatorRow(lat) {
  const clamped = Math.max(-85.05, Math.min(85.05, lat)) * D2R;
  return (1 - Math.log(Math.tan(clamped) + 1 / Math.cos(clamped)) / Math.PI) / 2;
}

export function eventGroup(cat) {
  return cat in EVENT_COLORS ? cat : 'outros';
}

/** Cartão do lugar ("mostra essa casa"): textos curtos e fotos só de servidores conhecidos, a de satélite primeiro. */
export function placeCardModel(card = {}) {
  const text = (value, limit) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);
  const safe = (url) => typeof url === 'string' && PLACE_PHOTO_HOSTS.some((host) => url.startsWith(host));
  const photos = [{ url: card.satellite, credit: 'Satélite · Esri, Maxar' }, ...(Array.isArray(card.photos) ? card.photos : [])]
    .filter((photo) => safe(photo?.url))
    .map((photo) => ({ url: photo.url, credit: text(photo.credit, 40) }))
    .slice(0, 9);
  return {
    title: text(card.title, 60) || 'Lugar',
    kind: text(card.kind, 30),
    address: text(card.address, 80),
    details: (Array.isArray(card.details) ? card.details : []).map((line) => text(line, 80)).filter(Boolean).slice(0, 4),
    photos,
  };
}

export const LAYER_NAMES = {
  terremotos: 'Terremotos', avioes: 'Aviões', satelites: 'Satélites', cameras: 'Câmeras', starlink: 'Starlink', lixo: 'Lixo espacial', navios: 'Navios',
  chuva: 'Chuva', raios: 'Raios', aurora: 'Sol e auroras', eventos: 'Queimadas e tempestades', asteroides: 'Asteroides', foto: 'Foto do dia',
  vento: 'Vento e nuvens', mare: 'Maré', bicicletas: 'Bicicletas', radios: 'Rádios',
};
// camadas que são um cartão do lado (não pontos no globo)
const CARD_LAYERS = ['asteroides', 'foto', 'vento', 'mare'];
// barra da direita: categorias -> camadas. '#x' = fonte que não existe aberta/ligada aqui: aparece como indisponível, sem dado falso
export const CATEGORIES = [
  ['Movimento', ['avioes', 'navios']], ['Espaço', ['satelites', 'starlink', 'lixo', 'aurora', 'asteroides', 'foto']],
  ['Eventos', ['terremotos', 'eventos', '#firms']], ['Clima', ['chuva', 'raios', 'vento', 'mare']],
  // Infra (energia, antenas e cabos) e ônibus ao vivo saíram: não há fonte aberta ao vivo para o Brasil.
  ['Cidade', ['bicicletas', 'radios', '#trafego']], ['Câmeras', ['cameras', '#mapillary']],
  ['Mapa', []], ['Pessoal', []],
];
const SOURCES = { terremotos: 'USGS', avioes: 'adsb.lol', satelites: 'CelesTrak', starlink: 'CelesTrak', lixo: 'CelesTrak', navios: 'aisstream.io',
  chuva: 'RainViewer', raios: 'Blitzortung', aurora: 'NOAA', eventos: 'NASA EONET', asteroides: 'NASA NeoWs', foto: 'NASA APOD',
  cameras: 'Windy Webcams', vento: 'Open-Meteo', mare: 'Open-Meteo Marine', bicicletas: 'CityBikes', radios: 'Radio Browser' };
const MISSING = {
  firms: ['Focos de calor (FIRMS)', 'precisa da chave grátis NASA FIRMS'], trafego: ['Trânsito', 'precisa da chave grátis TomTom'],
  onibus: ['Ônibus ao vivo', 'indisponível: sem GTFS ao vivo aberto aqui'], mapillary: ['Fotos de rua', 'indisponível: precisa do token Mapillary'],
  energia: ['Energia', 'indisponível: sem fonte aberta ao vivo'], telecom: ['Antenas e cabos', 'indisponível: sem fonte aberta ao vivo'],
};
const FAVS_KEY = 'kira.godseye.favs';
// menu sanfona: emoji e cor de cada categoria e camada
const CAT_LOOK = { Movimento: ['✈️', '#38bdf8'], Espaço: ['🛰️', '#a78bfa'], Eventos: ['🌋', '#fb7185'], Clima: ['⛅', '#22d3ee'],
  Cidade: ['🏙️', '#fbbf24'], Câmeras: ['📷', '#f472b6'], Infra: ['⚡', '#facc15'], Mapa: ['🗺️', '#34d399'], Pessoal: ['⭐', '#c084fc'] };
const LAYER_EMOJI = { avioes: '✈️', navios: '🚢', satelites: '🛰️', starlink: '✨', lixo: '🗑️', aurora: '🌌', asteroides: '☄️', foto: '🖼️',
  terremotos: '🌍', eventos: '🔥', chuva: '🌧️', raios: '⚡', vento: '💨', mare: '🌊', bicicletas: '🚲', radios: '📻', cameras: '📷' };
const CAM_KEY = 'kira.godseye.cam';
const hhmm = (d) => `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
/** Picos e vales da maré numa série [[hora, metros], ...]. */
export function tideTurns(rows) {
  return rows.filter((r, i) => i > 0 && i < rows.length - 1
    && ((r[1] >= rows[i - 1][1] && r[1] > rows[i + 1][1]) || (r[1] <= rows[i - 1][1] && r[1] < rows[i + 1][1])))
    .map((r, i, all) => ({ time: r[0], m: r[1], high: r[1] > (all[i - 1]?.[1] ?? all[i + 1]?.[1] ?? 0) }));
}
// Camadas de órbita: grupos do backend (CelesTrak) e a cor dos pontos. Starlink e lixo vêm numa amostra de 250.
const ORBIT_LAYERS = {
  satelites: { groups: ['estacoes', 'visiveis'], color: 0xe9d5ff, size: 0.02 },
  starlink: { groups: ['starlink'], color: 0x7dd3fc, size: 0.013 },
  lixo: { groups: ['lixo'], color: 0xfb923c, size: 0.013 },
};

/** Latitude/longitude -> ponto na esfera, no mesmo mapeamento da textura do THREE.SphereGeometry. */
export function toXYZ(lat, lon, radius = 1) {
  const phi = (lon + 180) * D2R;
  const theta = (90 - lat) * D2R;
  return [-radius * Math.cos(phi) * Math.sin(theta), radius * Math.cos(theta), radius * Math.sin(phi) * Math.sin(theta)];
}

/** Rotação do globo que põe (lat, lon) de frente para a câmera. */
export function facingRotation(lat, lon) {
  const [x, , z] = toXYZ(0, lon);
  return { x: lat * D2R, y: -Math.atan2(x, z) };
}

/** O contrário: o ponto da Terra que está no centro da tela para uma rotação do globo. */
export function centerOf(rotation) {
  const lat = Math.max(-85, Math.min(85, rotation.x / D2R));
  const lon = ((((-90 - rotation.y / D2R) % 360) + 540) % 360) - 180;
  return { lat, lon };
}

export function planeBucket(altFt) {
  return altFt >= 20000 ? 2 : altFt >= 5000 ? 1 : 0;
}

export function quakeBucket(mag) {
  return mag >= 5.5 ? 2 : mag >= 4 ? 1 : 0;
}

export function ago(timestamp, now = Date.now()) {
  const minutes = Math.max(0, Math.round((now - timestamp) / 60000));
  if (minutes < 60) return `há ${minutes} min`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `há ${hours} h` : `há ${Math.round(hours / 24)} dias`;
}

function el(doc, tag, className = '', text = '') {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function loadScript(doc, src) {
  return new Promise((resolve, reject) => {
    const script = doc.createElement('script');
    script.src = src;
    script.onload = resolve;
    script.onerror = () => reject(new Error(`não carregou ${src}`));
    doc.head.appendChild(script);
  });
}

export function createGodsEye({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  const doc = documentRef;
  const win = windowRef;
  let THREE = null;
  let renderer = null;
  let scene = null;
  let camera = null;
  let globe = null;
  let frameId = 0;
  let idleTimer = 0;
  let lastFrame = 0;
  let lastInput = 0;
  let open = false;
  let overlay = null;
  let ui = {};
  let focus = { ...DEFAULT_FOCUS };
  let target = facingRotation(focus.lat, focus.lon);
  let distance = 3.2;
  const active = new Set();
  const layers = {};
  const data = { terremotos: [], avioes: [], satelites: [], cameras: [], starlink: [], lixo: [], navios: [], chuva: [], raios: [], aurora: [], eventos: [], asteroides: [], foto: [], vento: [], mare: [], bicicletas: [], radios: [] };
  const stamp = {}; // hora da última leitura de cada camada
  const quakeFilter = { min: 0, depth: 700 };
  let space = null;
  let shipsNeedKey = false;
  let eventsByGroup = {};
  const timers = [];
  /** Camada que respondeu "precisa de chave": sai do menu (e a categoria some se ficar vazia). */
  function hideLayer(key) {
    const chip = sideChips[key];
    if (!chip || chip.hidden) return;
    if (active.has(key)) toggle(key, false);
    chip.hidden = true; chip.style.display = 'none'; // o CSS do chip/aba põe display e venceria o [hidden]
    const pane = chip.parentElement;
    if (pane && ![...pane.children].some((c) => c.dataset?.layer && !c.hidden)) {
      const tab = dockTabs.find(([, p]) => p === pane)?.[0];
      if (tab) { tab.hidden = true; tab.style.display = 'none'; }
    }
  }
  const satrecs = { satelites: [], starlink: [], lixo: [] };
  let mapView = null;
  let view3d = null;
  let camerasNeedKey = false;
  let flight = 0; // viagem atual; um pedido novo cancela a anterior no meio

  function savedTheme() {
    try {
      const name = win.localStorage?.getItem(THEME_KEY);
      return THEMES.includes(name) ? name : DEFAULT_THEME;
    } catch (_) {
      return DEFAULT_THEME;
    }
  }

  /** Reinicia a animação de "crescer" de um painel ou botão (abrir, trocar o conteúdo, clicar). */
  function grow(node) {
    if (!node) return;
    node.classList.remove('ge-grow');
    void node.offsetWidth;
    node.classList.add('ge-grow');
  }

  /** Cor do halo e da grade do globo vem do visual (variáveis --ge-globe-*). */
  function paintGlobe() {
    if (!overlay || !globeLook.halo) return;
    const css = win.getComputedStyle(overlay);
    const color = (name, fallback) => (css.getPropertyValue(name).trim() || fallback);
    globeLook.halo.material.color.set(color('--ge-globe-halo', '#9d4dff'));
    globeLook.grid.material.color.set(color('--ge-globe-grid', '#a855f7'));
    globeLook.stars.material.color.set(color('--ge-globe-stars', '#cbb8ff'));
  }
  const globeLook = {};

  function boot() {
    overlay.classList.remove('is-booting');
    void overlay.offsetWidth;
    overlay.classList.add('is-booting');
    if (camera) camera.position.z = BOOT_CAMERA_Z;
    win.clearTimeout(bootTimer);
    bootTimer = win.setTimeout(() => overlay.classList.remove('is-booting'), BOOT_MS);
  }
  let bootTimer = 0;

  const fetchJson = async (path) => {
    const response = await win.fetch(path, { cache: 'no-store' });
    return response.ok ? response.json() : { ok: false, items: [] };
  };

  function buildOverlay() {
    overlay = el(doc, 'div', 'gods-eye');
    overlay.dataset.geTheme = savedTheme();
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-label', 'Olho de Deus');
    const stage = el(doc, 'div', 'gods-eye-stage');
    const mapBox = el(doc, 'div', 'gods-eye-map');
    const box3d = el(doc, 'div', 'gods-eye-map gods-eye-3d');
    const head = el(doc, 'header', 'gods-eye-head');
    const title = el(doc, 'div', 'gods-eye-title', 'OLHO DE DEUS');
    const clock = el(doc, 'div', 'gods-eye-clock');
    const close = el(doc, 'button', 'gods-eye-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', 'Sair do Olho de Deus');
    close.addEventListener('click', () => api.close());
    head.append(title, clock, close);
    const chips = buildSide();
    const chipFor = sideChips;
    const info = el(doc, 'div', 'gods-eye-info');
    info.setAttribute('aria-live', 'polite');
    const hint = el(doc, 'div', 'gods-eye-hint', 'Arraste para girar · roda para aproximar (até as ruas) · 1-9 camadas · setas · Esc sai');
    const hud = el(doc, 'div', 'gods-eye-hud');
    hud.setAttribute('aria-hidden', 'true');
    const hudText = el(doc, 'div', 'gods-eye-hud-text');
    hud.append(el(doc, 'div', 'gods-eye-hud-reticle'), el(doc, 'div', 'gods-eye-hud-sweep'), hudText);
    const placeBox = el(doc, 'aside', 'gods-eye-place');
    placeBox.hidden = true;
    // ativação: anéis girando, varredura e o nome, por cima de tudo, só nos primeiros segundos
    const bootBox = el(doc, 'div', 'gods-eye-boot');
    bootBox.setAttribute('aria-hidden', 'true');
    const bootText = el(doc, 'div', 'gods-eye-boot-text');
    bootText.append(el(doc, 'strong', '', 'OLHO DE DEUS'), el(doc, 'span', '', 'SINCRONIZANDO SATÉLITES'), el(doc, 'i'));
    bootBox.append(el(doc, 'div', 'gods-eye-boot-ring r1'), el(doc, 'div', 'gods-eye-boot-ring r2'), el(doc, 'div', 'gods-eye-boot-ring r3'),
      el(doc, 'div', 'gods-eye-boot-scan'), bootText);
    overlay.append(stage, mapBox, box3d, head, chips, info, hint, hud, placeBox, bootBox);
    doc.body.appendChild(overlay);
    ui = { stage, clock, chipFor, info, mapBox, hint, hud, hudText, placeBox };
    mapView = createGodsEyeMap({
      container: mapBox, documentRef: doc, windowRef: win, fetchJson,
      quakeFilter: (q) => q.mag >= quakeFilter.min && (q.depth == null || q.depth <= quakeFilter.depth),
      onExit: (lat, lon) => leaveMap(lat, lon),
      onInfo: (key, count, needsKey) => {
        if (key === 'cameras' && needsKey && !camerasNeedKey) {
          camerasNeedKey = true;
          hideLayer('cameras');
        }
        mapCounts[key] = count;
        chipCounts();
      },
    });
    view3d = createGodsEye3d({ container: box3d, documentRef: doc, windowRef: win });
    stage.addEventListener('pointerdown', onPointerDown);
    stage.addEventListener('wheel', onWheel, { passive: true });
    stage.addEventListener('click', onPick);
  }

  // ---- barra da direita: busca, categorias, camadas com fonte e hora, filtros, mapa e favoritos ----
  const sideChips = {};
  let dockTabs = [];
  const button = (label, fn, className = 'gods-eye-chip') => {
    const b = el(doc, 'button', className, label);
    b.type = 'button';
    if (fn) b.addEventListener('click', fn);
    return b;
  };
  function buildSide() {
    const side = el(doc, 'aside', 'gods-eye-layers gods-eye-side');
    const search = doc.createElement('input');
    search.className = 'ge-search';
    search.placeholder = 'Buscar cidade, lugar ou CEP';
    search.setAttribute('aria-label', 'Buscar cidade, lugar ou CEP');
    const hints = el(doc, 'div', 'ge-suggest');
    let wait = 0;
    let found = [];
    const go = (p) => { hints.replaceChildren(); search.value = p.label; api.command({ action: 'focar', focus: p, zoom: 13 }); };
    search.addEventListener('input', () => {
      win.clearTimeout(wait);
      wait = win.setTimeout(async () => {
        const q = search.value.trim();
        found = q.length < 2 ? [] : ((await fetchJson(`/api/godseye/suggest?q=${encodeURIComponent(q)}&lat=${focus.lat}&lon=${focus.lon}`)).items || []);
        hints.replaceChildren(...(q.length >= 2 && !found.length ? [el(doc, 'div', 'ge-muted', 'Nada encontrado')]
          : found.map((p) => button(p.label, () => go(p), 'ge-hint'))));
      }, 280);
    });
    // as teclas do globo (1-9, setas, Esc) não valem enquanto digita
    search.addEventListener('keydown', (e) => { if (e.key === 'Enter' && found[0]) go(found[0]); if (e.key === 'Escape') search.blur(); });
    // Dock (coluna de ícones de categoria) + janela lateral (flyout) que abre ao lado com as camadas da categoria.
    const dock = el(doc, 'div', 'ge-dock');
    const flyout = el(doc, 'div', 'ge-flyout');
    const flyTitle = el(doc, 'div', 'ge-fly-title');
    const flyBody = el(doc, 'div', 'ge-fly-body');
    flyout.append(flyTitle, flyBody);
    let current = 0;
    try { current = Number(win.localStorage?.getItem('kira.godseye.tab')) || 0; } catch (_) { /* sem armazenamento */ }
    const all = CATEGORIES.map(([name, keys], index) => {
      const [emoji, color] = CAT_LOOK[name] || ['•', '#a78bfa'];
      const tab = button('', () => {
        current = current === index ? -1 : index; // tocar no aberto fecha a janela
        try { win.localStorage?.setItem('kira.godseye.tab', String(index)); } catch (_) { /* vale só agora */ }
        showTab();
      }, 'ge-dock-btn');
      tab.style.setProperty('--cat', color);
      tab.title = name;
      tab.setAttribute('aria-label', name);
      const count = keys.filter((k) => !k.startsWith('#')).length;
      tab.append(el(doc, 'span', 'ge-dock-emoji', emoji));
      if (count) tab.append(el(doc, 'span', 'ge-dock-count', String(count)));
      const pane = el(doc, 'div', 'ge-pane');
      pane.style.setProperty('--cat', color);
      keys.forEach((key) => {
        const chip = button('', null);
        chip.append(el(doc, 'span', 'ge-chip-name'), el(doc, 'span', 'ge-chip-meta'));
        chip.dataset.emoji = LAYER_EMOJI[key] || '•';
        if (key.startsWith('#')) return; // sem chave/sem fonte: fora do menu (2026-11-05)
        {
          chip.dataset.layer = key;
          chip.addEventListener('click', () => { grow(chip); toggle(key); });
          sideChips[key] = chip;
        }
        pane.append(chip);
        if (key === 'terremotos') pane.append(quakeFilters());
      });
      if (name === 'Mapa') mapPane(pane);
      if (name === 'Pessoal') favPane(pane);
      dock.append(tab);
      return [tab, pane, name, color];
    });
    dockTabs = all;
    fetchJson('/api/godseye/keys').then((r) => (r.missing || []).forEach(hideLayer)).catch(() => {});
    const showTab = () => {
      all.forEach(([tab], i) => { tab.classList.toggle('is-on', i === current); tab.setAttribute('aria-expanded', String(i === current)); });
      const sel = all[current];
      flyout.hidden = !sel;
      if (!sel) return;
      flyout.style.setProperty('--cat', sel[3]);
      flyTitle.textContent = `${CAT_LOOK[sel[2]]?.[0] || ''} ${sel[2]}`.trim();
      flyBody.replaceChildren(sel[1]);
      sel[1].hidden = false;
    };
    showTab();
    const top = el(doc, 'div', 'ge-dock-top');
    top.append(search, hints); // busca sempre visível, acima do dock
    const row = el(doc, 'div', 'ge-dock-row');
    row.append(flyout, dock); // janela lateral à esquerda, coluna de ícones à direita
    side.append(top, row);
    return side;
  }

  function slider(label, min, max, step, value, onInput) {
    const box = el(doc, 'label', 'ge-slider');
    const out = el(doc, 'span', 'ge-muted');
    const input = doc.createElement('input');
    Object.assign(input, { type: 'range', min, max, step, value });
    const show = () => { out.textContent = `${label}: ${String(input.value).replace('.', ',')}`; };
    input.addEventListener('input', () => { show(); onInput(Number(input.value)); });
    show();
    box.append(out, input);
    return box;
  }

  function quakeFilters() {
    const box = el(doc, 'div', 'ge-filters');
    const redraw = () => { if (active.has('terremotos')) { clearLayer('terremotos'); drawQuakes(); mapView?.redrawQuakes(); } chipCounts(); };
    box.append(slider('Magnitude mínima', 0, 7, 0.5, 0, (v) => { quakeFilter.min = v; redraw(); }),
      slider('Profundidade máx. (km)', 10, 700, 10, 700, (v) => { quakeFilter.depth = v; redraw(); }));
    return box;
  }

  function mapPane(pane) {
    const row = el(doc, 'div', 'ge-row');
    THEMES.forEach((name) => row.append(button(name, () => api.setTheme(name), 'ge-tab')));
    pane.append(button('Globo', () => api.command({ action: 'globo' })),
      button('Ruas aqui (satélite Esri)', () => { const p = here(); mapView?.isVisible ? mapView.focusOn(p.lat, p.lon, 16) : travel(p, 16); }),
      button('Prédios em 3D aqui', () => show3d(here())), el(doc, 'div', 'ge-muted', 'Visual'), row);
  }

  const readSaved = (key) => { try { return JSON.parse(win.localStorage?.getItem(key) || 'null'); } catch (_) { return null; } };
  const writeSaved = (key, value) => { try { win.localStorage?.setItem(key, JSON.stringify(value)); } catch (_) { /* vale só agora */ } };
  function favPane(pane) {
    const list = el(doc, 'div', 'ge-favs');
    const render = () => {
      const favs = readSaved(FAVS_KEY) || [];
      list.replaceChildren(...(favs.length ? favs.map((f, i) => {
        const row = el(doc, 'div', 'ge-row');
        const del = button('×', () => { favs.splice(i, 1); writeSaved(FAVS_KEY, favs); render(); }, 'ge-x');
        del.setAttribute('aria-label', `Apagar ${f.label}`);
        row.append(button(`★ ${f.label}`, () => api.command({ action: 'focar', focus: f, zoom: f.zoom || 0 })), del);
        return row;
      }) : [el(doc, 'div', 'ge-muted', 'Nenhum favorito ainda')]));
    };
    pane.append(button('☆ Favoritar este lugar', () => {
      const p = here();
      const favs = readSaved(FAVS_KEY) || [];
      favs.unshift({ lat: +p.lat.toFixed(5), lon: +p.lon.toFixed(5), label: p.label || `${p.lat.toFixed(3)}, ${p.lon.toFixed(3)}`, zoom: mapView?.isVisible ? 13 : 0 });
      writeSaved(FAVS_KEY, favs.slice(0, 20));
      render();
    }), button('Salvar posição da câmera', () => {
      writeSaved(CAM_KEY, { x: globe.rotation.x, y: globe.rotation.y, d: distance });
      setInfo(['Posição da câmera salva']);
    }), button('Voltar à posição salva', () => {
      const cam = readSaved(CAM_KEY);
      if (!cam) { setInfo(['Nenhuma posição salva']); return; }
      if (view3d?.isVisible) leave3d();
      if (mapView?.isVisible) { const c = mapView.center(); leaveMap(c.lat, c.lon); }
      target = { x: cam.x, y: cam.y };
      distance = cam.d;
      lastInput = Infinity;
    }), list);
    render();
  }

  function setInfo(lines) {
    ui.info.replaceChildren(...lines.map((line, index) => el(doc, index ? 'div' : 'strong', '', line)));
    grow(ui.info);
  }

  const mapCounts = {};

  function chipCounts() {
    for (const [key, chip] of Object.entries(ui.chipFor)) {
      const count = mapView?.isVisible && key in mapCounts ? mapCounts[key] : key === 'terremotos' ? shownQuakes().length : (data[key] || []).length;
      const extra = key === 'vento' || key === 'mare' ? '' : key === 'aurora' ? (space?.kp != null ? `Kp ${String(space.kp).replace('.', ',')}` : '')
        : key === 'chuva' || key === 'foto' ? '' : key === 'navios' && shipsNeedKey ? 'sem chave' : String(count);
      chip.classList.toggle('is-on', active.has(key));
      const n = Object.keys(LAYER_NAMES).indexOf(key) + 1;
      chip.firstChild.textContent = `${n <= 9 ? `${n} · ` : ''}${LAYER_NAMES[key]}${active.has(key) && extra ? ` (${extra})` : ''}`;
      chip.lastChild.textContent = active.has(key) ? `${SOURCES[key]} · ${stamp[key] ? hhmm(stamp[key]) : 'carregando'}` : SOURCES[key];
    }
  }

  // clientWidth: sem a escala da animação de entrada (com getBoundingClientRect o globo nascia menor e cortado)
  const stageSize = () => ({ width: ui.stage.clientWidth || win.innerWidth, height: ui.stage.clientHeight || win.innerHeight });
  // tela em pé (celular): o campo de visão segue a largura, senão a Terra não cabia de lado a lado
  const fov = (aspect) => (aspect >= 1 ? 42 : 2 * Math.atan(Math.tan((21 * Math.PI) / 180) / aspect) * (180 / Math.PI));

  function initScene() {
    THREE = win.THREE;
    const { width, height } = stageSize();
    renderer = new THREE.WebGLRenderer({ antialias: false, alpha: true });
    renderer.setPixelRatio(1);
    renderer.setSize(width, height);
    renderer.domElement.style.touchAction = 'none';
    ui.stage.appendChild(renderer.domElement);
    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(fov(width / height), width / height, 0.05, 100);
    camera.position.set(0, 0, distance);
    globe = new THREE.Group();
    globe.rotation.order = 'XYZ';
    scene.add(globe);
    // Nasce escuro e clareia quando a foto da Terra chega (antes aparecia uma bola branca por um instante).
    const phone = Boolean(win.KIRA_REMOTE);
    const earth = new THREE.Mesh(new THREE.SphereGeometry(1, phone ? 32 : 48, phone ? 24 : 32), new THREE.MeshBasicMaterial({ color: 0x0b1024 }));
    new THREE.TextureLoader().load(EARTH_TEXTURE, (texture) => {
      earth.material.map = texture;
      earth.material.needsUpdate = true;
      const from = earth.material.color.clone();
      const started = win.performance.now();
      const reveal = (now) => {
        const t = Math.min(1, (now - started) / GLOBE_REVEAL_MS);
        earth.material.color.setRGB(from.r + (1 - from.r) * t, from.g + (1 - from.g) * t, from.b + (1 - from.b) * t);
        if (t < 1) win.requestAnimationFrame(reveal);
      };
      win.requestAnimationFrame(reveal);
    });
    globe.add(earth);
    const halo = globeLook.halo = new THREE.Mesh(new THREE.SphereGeometry(1.035, phone ? 32 : 48, phone ? 20 : 32), new THREE.MeshBasicMaterial({ color: 0x9d4dff, transparent: true, opacity: 0.12, side: THREE.BackSide, blending: THREE.AdditiveBlending }));
    scene.add(halo);
    // Grade de latitude/longitude a cada 30°, para o ar de "sensor".
    const grid = [];
    for (let lat = -60; lat <= 60; lat += 30) for (let lon = -180; lon < 180; lon += 6) grid.push(...toXYZ(lat, lon, 1.002), ...toXYZ(lat, lon + 6, 1.002));
    for (let lon = -180; lon < 180; lon += 30) for (let lat = -84; lat < 84; lat += 6) grid.push(...toXYZ(lat, lon, 1.002), ...toXYZ(lat + 6, lon, 1.002));
    const gridGeometry = new THREE.BufferGeometry();
    gridGeometry.setAttribute('position', new THREE.Float32BufferAttribute(grid, 3));
    globeLook.grid = new THREE.LineSegments(gridGeometry, new THREE.LineBasicMaterial({ color: 0xa855f7, transparent: true, opacity: 0.18 }));
    globe.add(globeLook.grid);
    const stars = [];
    for (let i = 0; i < (phone ? 260 : 700); i += 1) {
      const r = 30 + Math.random() * 20;
      const u = Math.random() * 2 - 1;
      const a = Math.random() * Math.PI * 2;
      stars.push(r * Math.sqrt(1 - u * u) * Math.cos(a), r * u, r * Math.sqrt(1 - u * u) * Math.sin(a));
    }
    const starGeometry = new THREE.BufferGeometry();
    starGeometry.setAttribute('position', new THREE.Float32BufferAttribute(stars, 3));
    globeLook.stars = new THREE.Points(starGeometry, new THREE.PointsMaterial({ color: 0xcbb8ff, size: 0.12, sizeAttenuation: true }));
    scene.add(globeLook.stars);
    paintGlobe();
    globe.rotation.x = target.x;
    globe.rotation.y = target.y;
  }

  function points(name, positions, color, size) {
    if (layers[name]) {
      globe.remove(layers[name]);
      layers[name].geometry.dispose();
      layers[name].material.dispose();
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    const object = new THREE.Points(geometry, new THREE.PointsMaterial({ color, size, sizeAttenuation: true, transparent: true, opacity: 0.95, depthWrite: false }));
    object.name = name;
    object.userData.size = size;
    globe.add(object);
    layers[name] = object;
  }

  const shownQuakes = () => data.terremotos.filter((q) => q.mag >= quakeFilter.min && (q.depth == null || q.depth <= quakeFilter.depth));
  function drawQuakes() {
    const buckets = [[], [], []];
    shownQuakes().forEach((quake) => buckets[quakeBucket(quake.mag)].push(...toXYZ(quake.lat, quake.lon, 1.006)));
    [[0xfbbf24, 0.018], [0xf97316, 0.03], [0xef4444, 0.05]].forEach(([color, size], index) => {
      if (active.has('terremotos')) points(`terremotos${index}`, buckets[index], color, size);
    });
  }

  function drawPlanes() {
    if (!active.has('avioes')) return;
    // cor pela altitude: no chão/subindo (laranja), médio (ciano), cruzeiro (branco)
    [[0xfb923c, 0.014], [0x22d3ee, 0.016], [0xf0f9ff, 0.018]].forEach(([color, size], bucket) => {
      points(`avioes${bucket}`, data.avioes.filter((plane) => planeBucket(plane.alt_ft) === bucket)
        .flatMap((plane) => toXYZ(plane.lat, plane.lon, 1.004 + Math.min(plane.alt_ft, 45000) * 4e-7)), color, size);
    });
  }

  function drawLightning() {
    if (!active.has('raios')) return;
    // últimos 2 minutos: maiores e brancos; o resto (até 10 min) amarelo
    const cut = Date.now() / 1000 - 120;
    const fresh = [];
    const old = [];
    data.raios.forEach((strike) => (strike.t >= cut ? fresh : old).push(...toXYZ(strike.lat, strike.lon, 1.008)));
    points('raios0', old, 0xfacc15, 0.012);
    points('raios1', fresh, 0xfffbeb, 0.022);
  }

  function drawEvents() {
    if (!active.has('eventos')) return;
    const groups = {};
    data.eventos.forEach((event) => (groups[eventGroup(event.cat)] ||= []).push(event));
    eventsByGroup = groups;
    for (const [group, color] of Object.entries(EVENT_COLORS)) points(`eventos-${group}`, (groups[group] || []).flatMap((e) => toXYZ(e.lat, e.lon, 1.007)), color, 0.03);
  }

  function drawAurora() {
    if (!active.has('aurora')) return;
    const weak = [];
    const strong = [];
    data.aurora.forEach(([lat, lon, power]) => (power >= 25 ? strong : weak).push(...toXYZ(lat, lon, 1.012)));
    points('aurora0', weak, 0x22c55e, 0.012);
    points('aurora1', strong, 0x86efac, 0.02);
    layers.aurora0.material.opacity = 0.45;
    for (const name of ['aurora0', 'aurora1']) layers[name].material.blending = THREE.AdditiveBlending;
  }

  function drawShips() {
    if (!active.has('navios')) return;
    points('navios', data.navios.flatMap((ship) => toXYZ(ship.lat, ship.lon, 1.004)), 0x60a5fa, 0.014);
  }

  /** Radar de chuva na esfera: 16 quadros do mundo (Mercator) redesenhados linha a linha em latitude/longitude. */
  async function drawRain(rain) {
    const n = 2 ** RAIN_GLOBE_ZOOM;
    const mercator = doc.createElement('canvas');
    mercator.width = mercator.height = 256 * n;
    const mctx = mercator.getContext('2d');
    const tiles = [];
    for (let x = 0; x < n; x += 1) for (let y = 0; y < n; y += 1) tiles.push({ x, y });
    await Promise.all(tiles.map(({ x, y }) => new Promise((resolve) => {
      const img = new win.Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => { mctx.drawImage(img, x * 256, y * 256); resolve(); };
      img.onerror = resolve;
      img.src = rainTileUrl(rain, RAIN_GLOBE_ZOOM, x, y);
    })));
    if (!active.has('chuva') || !globe) return;
    const flat = doc.createElement('canvas');
    flat.width = mercator.width;
    flat.height = mercator.width / 2;
    const fctx = flat.getContext('2d');
    for (let row = 0; row < flat.height; row += 1) {
      const lat = 90 - ((row + 0.5) / flat.height) * 180;
      if (Math.abs(lat) > 85) continue;
      fctx.drawImage(mercator, 0, Math.floor(mercatorRow(lat) * mercator.height), mercator.width, 1, 0, row, flat.width, 1);
    }
    clearLayer('chuva');
    const texture = new THREE.CanvasTexture(flat);
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(1.005, 64, 48), new THREE.MeshBasicMaterial({ map: texture, transparent: true, opacity: 0.85, depthWrite: false }));
    mesh.name = 'chuva';
    mesh.userData.texture = texture;
    mesh.raycast = () => {};
    globe.add(mesh);
    layers.chuva = mesh;
  }

  /** Cartão do lado (asteroides, foto do dia): o mesmo quadro do cartão do lugar. */
  function showSideCard(title, lines, image = '', credit = '') {
    const box = ui.placeBox;
    const close = el(doc, 'button', 'gods-eye-place-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', 'Fechar cartão');
    close.addEventListener('click', () => { box.hidden = true; });
    const nodes = [close];
    if (APOD_IMAGE_RE.test(image)) {
      const img = doc.createElement('img');
      img.className = 'gods-eye-place-photo';
      img.src = image;
      img.alt = title;
      nodes.push(img, el(doc, 'div', 'gods-eye-place-credit', credit));
    }
    nodes.push(el(doc, 'strong', 'gods-eye-place-title', title), ...lines.filter(Boolean).map((line) => el(doc, 'div', 'gods-eye-place-line', line)));
    box.replaceChildren(...nodes);
    box.hidden = false;
    grow(box);
  }

  const thousands = (n) => Number(n || 0).toLocaleString('pt-BR');

  function drawSatellites(now = new Date()) {
    const sat = win.satellite;
    if (!sat) return;
    const gmst = sat.gstime(now);
    for (const [key, spec] of Object.entries(ORBIT_LAYERS)) {
      if (!active.has(key) || !satrecs[key].length) continue;
      const positions = [];
      data[key] = [];
      for (const item of satrecs[key]) {
        const pv = sat.propagate(item.rec, now);
        if (!pv.position) continue;
        const geo = sat.eciToGeodetic(pv.position, gmst);
        const lat = sat.degreesLat(geo.latitude);
        const lon = sat.degreesLong(geo.longitude);
        data[key].push({ name: item.name, lat, lon, alt_km: geo.height });
        positions.push(...toXYZ(lat, lon, 1 + geo.height / 6371));
      }
      points(key, positions, spec.color, spec.size);
    }
  }

  function clearLayer(key) {
    for (const name of Object.keys(layers).filter((n) => n.startsWith(key))) {
      globe.remove(layers[name]);
      layers[name].geometry.dispose();
      layers[name].material.dispose();
      layers[name].userData.texture?.dispose();
      delete layers[name];
    }
  }

  async function loadLayer(key) {
    if (key === 'cameras') {
      if (!mapView?.isVisible) setInfo(['Câmeras públicas', 'Aparecem no mapa: aproxime o globo até a cidade.']);
    } else if (key === 'terremotos') {
      data.terremotos = (await fetchJson('/api/godseye/earthquakes')).items || [];
      drawQuakes();
    } else if (key === 'avioes') {
      data.avioes = (await fetchJson(`/api/godseye/planes?lat=${focus.lat}&lon=${focus.lon}&radius_km=${focus.radius || 250}`)).items || [];
      drawPlanes();
    } else if (key === 'raios') {
      data.raios = (await fetchJson('/api/godseye/lightning')).items || [];
      drawLightning();
    } else if (key === 'navios') {
      const answer = await fetchJson(`/api/godseye/ships?lat=${focus.lat}&lon=${focus.lon}&radius_km=${focus.radius || 250}`);
      shipsNeedKey = Boolean(answer.needs_key);
      if (shipsNeedKey) hideLayer('navios');
      data.navios = answer.items || [];
      drawShips();
    } else if (key === 'eventos') {
      data.eventos = (await fetchJson('/api/godseye/eonet')).items || [];
      drawEvents();
    } else if (key === 'aurora') {
      space = (await fetchJson('/api/godseye/space')).items || null;
      data.aurora = space?.aurora || [];
      drawAurora();
      if (space?.kp != null) setInfo(['Clima espacial (NOAA)', `Índice Kp ${String(space.kp).replace('.', ',')} · ${space.label}`, 'Verde = onde pode haver aurora agora']);
    } else if (key === 'chuva') {
      const rain = (await fetchJson('/api/godseye/rain')).items;
      if (rain?.host) {
        data.chuva = [rain];
        await drawRain(rain);
        const at = new Date(rain.time * 1000);
        setInfo(['Radar de chuva', `Atualizado às ${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')} · RainViewer`]);
      }
    } else if (key === 'asteroides') {
      const rocks = (await fetchJson('/api/godseye/neo')).items || [];
      data.asteroides = rocks;
      if (active.has('asteroides')) {
        showSideCard(`${rocks.length} asteroides passando perto (48 h)`, rocks.slice(0, 8).map((rock) =>
          `${rock.name} · ${thousands(rock.km)} km (${String(rock.lunar).replace('.', ',')}× a Lua) · ~${rock.size_m} m · ${thousands(rock.speed_kmh)} km/h`));
      }
    } else if (key === 'foto') {
      const apod = (await fetchJson('/api/godseye/apod')).items;
      data.foto = apod ? [apod] : [];
      if (!apod) setInfo(['Foto do dia da NASA', 'Indisponível agora (a chave de teste da NASA tem limite por dia: NASA_API_KEY no .env).']);
      else if (active.has('foto')) showSideCard(apod.title, [apod.date.split('-').reverse().join('/'), apod.text], apod.image, apod.credit ? `© ${apod.credit}` : 'NASA APOD');
    } else if (key === 'vento' || key === 'mare') {
      const got = await fetchJson(`/api/godseye/local?lat=${focus.lat}&lon=${focus.lon}`);
      const w = got.items?.weather;
      const tide = got.items?.tide;
      if (!got.ok || !w) throw new Error('fonte');
      data[key] = [got.items];
      const where = focus.label || 'aqui';
      if (key === 'vento') {
        const dirs = ['N', 'NE', 'L', 'SE', 'S', 'SO', 'O', 'NO'];
        showSideCard(`Vento e nuvens · ${where}`, [`Vento ${Math.round(w.wind_speed_10m)} km/h do ${dirs[Math.round(w.wind_direction_10m / 45) % 8]} · rajadas ${Math.round(w.wind_gusts_10m)} km/h`,
          `Nuvens cobrindo ${w.cloud_cover}% do céu`, `${String(w.temperature_2m).replace('.', ',')} °C`, `Open-Meteo · medido às ${w.time.slice(11, 16)}`]);
      } else if (!tide) showSideCard(`Maré · ${where}`, ['Indisponível: o ponto focado não fica no mar.']);
      else {
        showSideCard(`Maré · ${where}`, [...tideTurns(tide).slice(0, 5).map((t) => `${t.high ? 'Cheia' : 'Baixa'} ${t.time.slice(8, 10)}/${t.time.slice(5, 7)} às ${t.time.slice(11, 16)} · ${t.m.toFixed(2).replace('.', ',')} m`),
          'Open-Meteo Marine (previsão de modelo, não marégrafo)']);
      }
    } else if (key === 'bicicletas') {
      const got = (await fetchJson(`/api/godseye/bikes?lat=${focus.lat}&lon=${focus.lon}`)).items;
      data.bicicletas = got?.items || [];
      if (!got) setInfo(['Bicicletas', 'Nenhuma rede de bike compartilhada a até 60 km daqui (CityBikes).']);
      else setInfo([got.name, `${data.bicicletas.length} estações · ${data.bicicletas.reduce((a, b) => a + (b.free || 0), 0)} bikes livres agora · CityBikes`]);
      if (active.has('bicicletas')) points('bicicletas', data.bicicletas.flatMap((b) => toXYZ(b.lat, b.lon, 1.003)), 0x34d399, 0.01);
    } else if (key === 'radios') {
      data.radios = (await fetchJson(`/api/godseye/radios?lat=${focus.lat}&lon=${focus.lon}`)).items || [];
      if (!data.radios.length) setInfo(['Rádios', 'Nenhuma rádio com local cadastrado perto (Radio Browser).']);
      if (active.has('radios')) points('radios', data.radios.flatMap((r) => toXYZ(r.lat, r.lon, 1.005)), 0xfacc15, 0.016);
    } else if (key in ORBIT_LAYERS) {
      if (!win.satellite) await loadScript(doc, SATELLITE_JS);
      if (!satrecs[key].length) {
        const groups = await Promise.all(ORBIT_LAYERS[key].groups.map((group) => fetchJson(`/api/godseye/satellites?group=${group}`)));
        satrecs[key] = groups.flatMap((g) => g.items || []).map((tle) => ({ name: tle.name, rec: win.satellite.twoline2satrec(tle.line1, tle.line2) }));
      }
      drawSatellites();
    }
    stamp[key] = new Date();
    chipCounts();
  }

  function toggle(key, on = !active.has(key)) {
    if (on) {
      active.add(key);
      loadLayer(key).catch(() => { setInfo([`${LAYER_NAMES[key]} indisponível agora`, `${SOURCES[key]} não respondeu`]); active.delete(key); chipCounts(); });
    } else {
      active.delete(key);
      clearLayer(key);
      if (CARD_LAYERS.includes(key) && ui.placeBox) ui.placeBox.hidden = true;
      chipCounts();
    }
    mapView?.setLayers([...active]);
  }

  /** Do globo para o mapa de satélite (ruas, casas), no mesmo ponto. */
  async function enterMap(lat, lon, zoom = ENTER_ZOOM, label = '') {
    const flightAtStart = flight;
    try {
      mapView.setLayers([...active]);
      await mapView.show(lat, lon, zoom);
      if (!open || flightAtStart !== flight) {
        mapView.hide();
        return;
      }
      if (label) mapView.mark(lat, lon, label);
      ui.stage.classList.add('is-behind-map');
      ui.hint.textContent = 'Arraste para mover · roda aproxima até as ruas · afaste para voltar ao globo · Esc sai';
      focus = { ...focus, lat, lon };
    } catch (_) {
      setInfo(['Mapa de satélite indisponível agora', 'Continuo no globo.']);
    }
  }

  const pause = (ms) => new Promise((resolve) => win.setTimeout(resolve, ms));

  function showHud(place, status) {
    const lat = `${Math.abs(place.lat).toFixed(3)}°${place.lat < 0 ? 'S' : 'N'}`;
    const lon = `${Math.abs(place.lon).toFixed(3)}°${place.lon < 0 ? 'O' : 'L'}`;
    ui.hudText.replaceChildren(el(doc, 'strong', '', status), el(doc, 'div', '', place.label || 'Coordenadas'), el(doc, 'div', '', `${lat}  ${lon}`));
    ui.hud.classList.add('is-on');
  }

  /**
   * Viagem de filme: o mapa se afasta até o continente, vira globo, o globo recua e gira até o lugar novo, aproxima e,
   * se pediram ruas, mergulha no mapa de satélite lá. Antes o mapa ficava parado na rua antiga (só o título mudava).
   */
  async function travel(place, zoom) {
    const id = ++flight;
    showHud(place, 'RASTREANDO ALVO');
    if (ui.placeBox) ui.placeBox.hidden = true; // o cartão era do lugar antigo
    if (view3d?.isVisible) leave3d();
    if (mapView?.isVisible) {
      await mapView.pullBack();
      if (id !== flight) return;
      const here = mapView.center();
      leaveMap(here.lat, here.lon);
    }
    distance = 3.0;
    await pause(700);
    if (id !== flight) return;
    target = facingRotation(place.lat, place.lon);
    // caminho mais curto: o giro ocioso acumula voltas e o foco não deve desenrolá-las
    target.y = globe.rotation.y + Math.atan2(Math.sin(target.y - globe.rotation.y), Math.cos(target.y - globe.rotation.y));
    lastInput = Infinity; // lugar pedido fica parado no centro até o Chefe mexer no globo
    await pause(1500);
    if (id !== flight) return;
    showHud(place, 'ALVO TRAVADO');
    distance = zoom >= ENTER_ZOOM ? 1.36 : 1.9;
    await pause(zoom >= ENTER_ZOOM ? 1100 : 600);
    if (id !== flight) return;
    if (zoom >= ENTER_ZOOM) await enterMap(place.lat, place.lon, zoom, place.label);
    if (id === flight) ui.hud.classList.remove('is-on');
  }

  function showPlaceCard(card) {
    const model = placeCardModel(card);
    const box = ui.placeBox;
    const big = doc.createElement('img');
    big.className = 'gods-eye-place-photo';
    big.alt = model.title;
    const credit = el(doc, 'div', 'gods-eye-place-credit');
    const pick = (photo) => { big.src = photo.url; credit.textContent = photo.credit; };
    const thumbs = el(doc, 'div', 'gods-eye-place-thumbs');
    model.photos.forEach((photo, index) => {
      const button = el(doc, 'button', 'gods-eye-place-thumb');
      button.type = 'button';
      button.setAttribute('aria-label', `Foto ${index + 1}`);
      const img = doc.createElement('img');
      img.src = photo.url;
      img.alt = '';
      img.loading = 'lazy';
      button.appendChild(img);
      button.addEventListener('click', () => pick(photo));
      thumbs.appendChild(button);
    });
    const close = el(doc, 'button', 'gods-eye-place-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', 'Fechar cartão do lugar');
    close.addEventListener('click', () => { box.hidden = true; });
    const lines = [model.kind, model.address !== model.title ? model.address : '', ...model.details].filter(Boolean);
    box.replaceChildren(close, big, credit, model.photos.length > 1 ? thumbs : el(doc, 'span'), el(doc, 'strong', 'gods-eye-place-title', model.title),
      ...lines.map((line) => el(doc, 'div', 'gods-eye-place-line', line)));
    if (model.photos.length) pick(model.photos[0]);
    box.hidden = false;
    grow(box);
  }

  /** O ponto que está no centro agora: o mapa de perto, se aberto, ou o lugar focado no globo. */
  function here() {
    const c = mapView?.isVisible ? mapView.center() : null;
    return c ? { lat: c.lat, lon: c.lon, label: focus.label } : { lat: focus.lat, lon: focus.lon, label: focus.label };
  }

  async function show3d(place = here()) {
    try {
      await view3d.show(place.lat, place.lon, place.label);
      ui.stage.classList.add('is-behind-map');
      ui.hint.textContent = '3D · arraste para girar a câmera · roda aproxima · Esc volta ao mapa';
    } catch (_) {
      setInfo(['Visão 3D indisponível agora', 'Continuo no mapa.']);
    }
  }

  function leave3d() {
    view3d.hide();
    if (!mapView?.isVisible) ui.stage.classList.remove('is-behind-map');
    ui.hint.textContent = mapView?.isVisible
      ? 'Arraste para mover · roda aproxima até as ruas · afaste para voltar ao globo · Esc sai'
      : 'Arraste para girar · roda para aproximar (até as ruas) · 1-9 camadas · setas · Esc sai';
  }

  function leaveMap(lat, lon) {
    mapView.hide();
    ui.stage.classList.remove('is-behind-map');
    ui.hint.textContent = 'Arraste para girar · roda para aproximar (até as ruas) · 1-9 camadas · setas · Esc sai';
    target = facingRotation(lat, lon);
    if (globe) target.y = globe.rotation.y + Math.atan2(Math.sin(target.y - globe.rotation.y), Math.cos(target.y - globe.rotation.y));
    globe.rotation.x = target.x;
    globe.rotation.y = target.y;
    // o globo volta colado no ponto e se afasta suave enquanto o mapa some
    camera.position.z = 1.36;
    distance = 2.2;
    lastInput = Infinity;
    chipCounts();
  }

  function onPointerDown(event) {
    const start = { x: event.clientX, y: event.clientY, rx: globe.rotation.x, ry: globe.rotation.y };
    lastInput = performance.now();
    const move = (e) => {
      globe.rotation.y = start.ry + (e.clientX - start.x) * 0.005;
      globe.rotation.x = Math.max(-1.3, Math.min(1.3, start.rx + (e.clientY - start.y) * 0.005));
      target = { x: globe.rotation.x, y: globe.rotation.y };
      lastInput = performance.now();
    };
    const up = () => { win.removeEventListener('pointermove', move); win.removeEventListener('pointerup', up); };
    win.addEventListener('pointermove', move);
    win.addEventListener('pointerup', up);
  }

  function onWheel(event) {
    // O Leaflet administra a própria roda quando o mapa está visível. Deixar
    // este listener do globo processar o mesmo evento causa zoom concorrente e
    // é uma das condições que deixava callbacks antigos após o fechamento.
    if (mapView?.isVisible) return;
    // no limite do globo, continuar aproximando troca para o mapa de satélite no ponto do centro
    if (event.deltaY < 0 && distance <= 1.36) {
      const here = centerOf(globe.rotation);
      enterMap(here.lat, here.lon);
      return;
    }
    distance = Math.max(1.35, Math.min(6, distance * (event.deltaY > 0 ? 1.1 : 0.9)));
    lastInput = performance.now();
  }

  function onKey(event) {
    if (!open || event.target?.classList?.contains('ge-search')) return; // digitando na busca
    const keys = { ArrowLeft: [0, -0.15], ArrowRight: [0, 0.15], ArrowUp: [-0.12, 0], ArrowDown: [0.12, 0] };
    if (event.key === 'Escape') {
      if (view3d?.isVisible) leave3d();
      else api.close();
    }
    else if (keys[event.key]) {
      target = { x: Math.max(-1.3, Math.min(1.3, target.x + keys[event.key][0])), y: target.y + keys[event.key][1] };
      lastInput = performance.now();
    } else if (mapView?.isVisible && ['+', '=', '-'].includes(event.key)) mapView.zoomBy(event.key === '-' ? -1 : 1);
    else if (event.key === '+' || event.key === '=') {
      if (distance <= 1.36) enterMap(centerOf(globe.rotation).lat, centerOf(globe.rotation).lon);
      else distance = Math.max(1.35, distance * 0.9);
    } else if (event.key === '-') distance = Math.min(6, distance * 1.1);
    else if (/^[1-9]$/.test(event.key)) toggle(Object.keys(LAYER_NAMES)[Number(event.key) - 1]);
    else return;
    event.preventDefault();
  }

  function onPick(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    const mouse = new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.params.Points.threshold = 0.02 * distance;
    ray.setFromCamera(mouse, camera);
    const hit = ray.intersectObjects(Object.values(layers), false)[0];
    if (!hit) return;
    const name = hit.object.name;
    if (name.startsWith('terremotos')) {
      const bucket = Number(name.slice(-1));
      const quake = shownQuakes().filter((q) => quakeBucket(q.mag) === bucket)[hit.index];
      if (quake) setInfo([`Terremoto M${quake.mag}`, quake.place, `${ago(quake.time)}${quake.depth != null ? ` · ${quake.depth} km de profundidade` : ''} · USGS`]);
    } else if (name.startsWith('avioes')) {
      const plane = data.avioes.filter((p) => planeBucket(p.alt_ft) === Number(name.slice(-1)))[hit.index];
      if (plane) setInfo([`Voo ${plane.callsign || '?'}`, `${plane.type || 'aeronave'} · ${plane.alt_ft.toLocaleString('pt-BR')} pés`, `${plane.speed_kt} nós · rumo ${plane.track}°`]);
    } else if (name.startsWith('eventos-')) {
      const item = (eventsByGroup[name.slice(8)] || [])[hit.index];
      if (item) setInfo([item.title, `${item.kind} · NASA EONET`, item.date ? `desde ${item.date.split('-').reverse().join('/')}` : '']);
    } else if (name.startsWith('raios')) {
      const cut = Date.now() / 1000 - 120;
      const strike = data.raios.filter((s) => (name === 'raios1' ? s.t >= cut : s.t < cut))[hit.index];
      if (strike) setInfo(['Raio', ago(strike.t * 1000), `${strike.lat.toFixed(2)}, ${strike.lon.toFixed(2)} · Blitzortung`]);
    } else if (name === 'navios') {
      const ship = data.navios[hit.index];
      if (ship) setInfo([ship.name || `Navio ${ship.mmsi}`, `${ship.speed_kt ?? 0} nós · rumo ${ship.course ?? '?'}°`, ship.dest ? `destino ${ship.dest}` : `MMSI ${ship.mmsi}`]);
    } else if (name === 'bicicletas') {
      const b = data.bicicletas[hit.index];
      if (b) setInfo([b.name, `${b.free ?? '?'} bikes livres · ${b.slots ?? '?'} vagas`, `CityBikes${stamp.bicicletas ? ` · ${hhmm(stamp.bicicletas)}` : ''}`]);
    } else if (name === 'radios') {
      const r = data.radios[hit.index];
      if (r) setInfo([r.name, 'Rádio · Radio Browser']);
    } else if (name in ORBIT_LAYERS) {
      const sat = data[name][hit.index];
      if (sat) setInfo([sat.name, `${LAYER_NAMES[name]} · altitude ${Math.round(sat.alt_km)} km`]);
    }
  }

  function frame(now) {
    frameId = 0;
    if (!open) return;
    // com o mapa por cima o globo não precisa ser desenhado (economiza o processador)
    const globeVisible = !doc.hidden && !mapView?.isVisible && !view3d?.isVisible;
    if (globeVisible && (now - lastInput < INPUT_SMOOTH_MS || now - lastFrame >= frameMsNow() - 1)) {
      lastFrame = now;
      if (now - lastInput > IDLE_SPIN_AFTER_MS) target.y += 0.0012;
      globe.rotation.x += (target.x - globe.rotation.x) * 0.08;
      globe.rotation.y += (target.y - globe.rotation.y) * 0.08;
      camera.position.z += (distance - camera.position.z) * 0.1;
      // perto do chão os pontos cresciam até virar quadrados enormes (aviões sobre São Paulo): encolhem com a câmera
      const shrink = Math.min(1, Math.max(0.25, (camera.position.z - 1) / 2.2));
      for (const layer of Object.values(layers)) if (layer.userData.size) layer.material.size = layer.userData.size * shrink;
      renderer.render(scene, camera);
    }
    if (!globeVisible) { // globo coberto (mapa/3D) ou janela oculta: confere 4x/s em vez de acordar a cada quadro
      idleTimer = win.setTimeout(() => { idleTimer = 0; if (open && !frameId) frameId = win.requestAnimationFrame(frame); }, 250);
      return;
    }
    frameId = win.requestAnimationFrame(frame);
  }

  function holdOrb(hold) {
    (win.kira3D || win.__kira3dInstance)?.hold?.('godseye', Boolean(hold));
    // Olho de Deus aberto: só ele desenha. Orbe alternativo pausado e a tela de trás escondida (o navegador para de
    // pintar o que está com visibility hidden); ao fechar volta tudo (2026-10-31: travava muito).
    try {
      const userPaused = win.localStorage?.getItem('KIRA_ORB_PAUSED') === 'true';
      win.KiraAppearance?.setPaused?.(Boolean(hold) || userPaused);
    } catch (_) { /* sem aparência */ }
    doc.body?.classList?.toggle('kira-godseye-open', Boolean(hold));
  }

  const onResize = () => {
    if (!renderer) return;
    const { width, height } = stageSize();
    renderer.setSize(width, height);
    camera.aspect = width / height;
    camera.fov = fov(camera.aspect);
    camera.updateProjectionMatrix();
  };

  const api = {
    get isOpen() { return open; },
    open(initial = ['terremotos', 'satelites', 'avioes']) {
      if (!win.THREE) return { ok: false, error: 'three_indisponivel' };
      const opening = !open;
      if (!open) {
        if (!overlay) buildOverlay();
        overlay.hidden = false;
        doc.documentElement.classList.add('gods-eye-active');
        open = true;
        holdOrb(true);
        if (!renderer) initScene();
        boot();
        win.addEventListener('keydown', onKey, true);
        win.addEventListener('resize', onResize);
        timers.push(win.setInterval(() => !mapView?.isVisible && !view3d?.isVisible && drawSatellites(), 4000));
        // relógio e lugar por timer próprio: no mapa o globo não é desenhado
        timers.push(win.setInterval(() => { ui.clock.textContent = `${new Date().toISOString().slice(11, 19)} UTC${focus.label ? ` · ${focus.label}` : ''}`; }, 1000));
        timers.push(win.setInterval(() => active.has('avioes') && loadLayer('avioes').catch(() => {}), PLANES_EVERY_MS));
        timers.push(win.setInterval(() => active.has('terremotos') && loadLayer('terremotos').catch(() => {}), QUAKES_EVERY_MS));
        timers.push(win.setInterval(() => active.has('raios') && loadLayer('raios').catch(() => {}), LIGHTNING_EVERY_MS));
        timers.push(win.setInterval(() => active.has('navios') && loadLayer('navios').catch(() => {}), SHIPS_EVERY_MS));
        timers.push(win.setInterval(() => active.has('chuva') && loadLayer('chuva').catch(() => {}), RAIN_EVERY_MS));
        setInfo([`${win.matchMedia?.('(pointer: coarse)')?.matches ? 'Toque' : 'Clique'} num ponto para ver detalhes`]);
        frameId = win.requestAnimationFrame(frame);
      }
      // Abrindo: as camadas (busca + cálculo dos satélites) esperam a entrada terminar; antes disputavam o quadro e a
      // animação cortava (2026-11-05).
      const load = () => { if (!open) return; initial.forEach((key) => !active.has(key) && toggle(key, true)); chipCounts(); };
      if (opening) timers.push(win.setTimeout(load, 1300)); else load();
      return { ok: true };
    },
    /** Troca o visual (prévia dos modelos) e mostra a ativação de novo. */
    setTheme(name) {
      if (!THEMES.includes(name)) return { ok: false };
      try { win.localStorage?.setItem(THEME_KEY, name); } catch (_) { /* sem armazenamento: vale só agora */ }
      if (overlay) {
        overlay.dataset.geTheme = name;
        paintGlobe();
        if (open) boot();
      }
      return { ok: true };
    },
    showPlace(card) {
      if (!open || !ui.placeBox) return false;
      showPlaceCard(card);
      return true;
    },
    close() {
      if (!open) return { ok: true };
      // Invalida viagens/entradas de mapa que ainda estejam aguardando rede ou
      // timers antes de desmontar o container Leaflet.
      flight += 1;
      open = false;
      if (ui.placeBox) ui.placeBox.hidden = true;
      doc.documentElement.classList.remove('gods-eye-active');
      if (frameId) win.cancelAnimationFrame(frameId);
      frameId = 0;
      if (idleTimer) win.clearTimeout(idleTimer);
      idleTimer = 0;
      timers.splice(0).forEach((id) => win.clearInterval(id));
      win.removeEventListener('keydown', onKey, true);
      win.removeEventListener('resize', onResize);
      // Libera a memória do WebGL e o mapa: o globo some de verdade até abrir de novo.
      // Fechar o módulo precisa liberar também tiles, listeners e contexto WebGL;
      // apenas ocultar deixava o globo e o mapa consumindo memória entre aberturas.
      mapView?.destroy?.();
      view3d?.destroy?.();
      if (overlay) overlay.hidden = true;
      ui.stage?.classList.remove('is-behind-map');
      Object.keys(layers).forEach((name) => clearLayer(name));
      active.clear();
      renderer?.dispose();
      renderer?.forceContextLoss?.();
      renderer?.domElement.remove();
      renderer = null;
      scene = null;
      camera = null;
      globe = null;
      holdOrb(false);
      return { ok: true };
    },
    command(cmd = {}) {
      if (cmd.action === 'fechar') return api.close();
      if (cmd.action === 'globo' && view3d?.isVisible) leave3d();
      if (cmd.action === 'globo' && mapView?.isVisible) {
        const c = mapView.center();
        leaveMap(c.lat, c.lon);
      }
      if (cmd.focus) focus = { lat: cmd.focus.lat, lon: cmd.focus.lon, label: String(cmd.focus.label || ''), radius: cmd.radius_km };
      const requested = Array.isArray(cmd.layers) ? cmd.layers.filter((key) => key in LAYER_NAMES) : [];
      const result = api.open(cmd.action === 'abrir' && !requested.length ? undefined : []);
      if (!result.ok) return result;
      // camadas pedidas valem também junto com 'focar' ("mostra apenas aviões e foca em Fortaleza"): ficam só elas
      if (cmd.action === 'camadas' || requested.length) Object.keys(data).forEach((key) => !requested.includes(key) && active.has(key) && toggle(key, false));
      requested.forEach((key) => (['avioes', 'navios', 'vento', 'mare', 'bicicletas', 'radios'].includes(key) && active.has(key) ? loadLayer(key) : !active.has(key) && toggle(key, true)));
      for (const key of ['avioes', 'navios', 'vento', 'mare', 'bicicletas', 'radios']) if (cmd.focus && active.has(key) && !requested.includes(key)) loadLayer(key).catch(() => {});
      // "mostra as ruas de Fortaleza": viaja até lá e mergulha no mapa de satélite no zoom pedido
      const trip = cmd.focus ? travel(focus, cmd.action === '3d' ? 0 : Math.min(18, Number(cmd.zoom) || 0)) : Promise.resolve();
      trip.catch(() => ui.hud.classList.remove('is-on'));
      // "mostra em 3D": prédios em volume girando em volta do ponto (depois da viagem, se pediram outro lugar)
      if (cmd.action === '3d') {
        const place = cmd.focus ? { ...focus } : here();
        trip.then(() => show3d(place));
        return { ok: true, mode: '3d', layers: [...active], focus: { ...place, radius: focus.radius || 250 } };
      }
      // "mostra essa casa / o comércio X": vai até o ponto bem de perto; o backend manda o cartão (showPlace)
      if (cmd.action === 'lugar') {
        const place = cmd.focus ? { ...focus } : here();
        if (!cmd.focus) mapView?.isVisible ? mapView.focusOn(place.lat, place.lon, 18) : travel(place, 18).catch(() => ui.hud.classList.remove('is-on'));
        return { ok: true, mode: 'lugar', layers: [...active], focus: { ...place, radius: focus.radius || 250 } };
      }
      // os números reais o backend conta direto das fontes: aqui as camadas ainda estão carregando
      return { ok: true, map: Boolean(cmd.focus && Number(cmd.zoom) >= ENTER_ZOOM), layers: [...active], focus: { lat: focus.lat, lon: focus.lon, label: focus.label, radius: focus.radius || 250 } };
    },
  };
  return api;
}

if (globalThis.window && globalThis.document?.body && !globalThis.window.KiraGodsEye) {
  globalThis.window.KiraGodsEye = createGodsEye();
}
