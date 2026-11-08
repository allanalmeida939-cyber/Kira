// Rota da Kira: mapa com zoom (ruas recoloridas, fundo transparente), rota marcada de A até B com caminhos
// alternativos, minutos, lugares e todos os jeitos de ir (carro, moto, bike, a pé, ônibus com as linhas).
// Quatro modelos de verdade diferentes (layout, material, entrada do painel e desenho da rota):
//   cockpit    — cartão de vidro escuro: mapa à esquerda, coluna de dados à direita; abre desdobrando de uma linha
//   holograma  — sem cartão, tudo flutua sobre a tela; projeta de baixo para cima; a rota corre como um cometa
//   itinerario — folha translúcida em pilha (minutos, mapa, linha do tempo, cartões); sobe como uma folha; rota tracejada
//   radar      — lente redonda com anel de mira; abre como uma íris; a rota acende junto da varredura
// Chamado pelo backend (services/routes_api.py) via KiraRouteOverlay.show({id, model}); a prévia usa os mesmos arquivos.
(function () {
  const LEAFLET_JS = 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js';
  const LEAFLET_CSS = 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css';
  const STREETS = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}';
  // fusao1-4: o Cockpit (coluna de dados) junto com o Holograma (transparente, projetado), cada um de um jeito
  const MODELS = ['cockpit', 'holograma', 'itinerario', 'radar', 'fusao1', 'fusao2', 'fusao3', 'fusao4'];
  const AUTO_HIDE_MS = 3 * 60000;
  const MODE_NAME = { carro: 'Carro', moto: 'Moto', bike: 'Bike', a_pe: 'A pé', onibus: 'Ônibus' };
  // ruas: cor e força de cada modelo (o fundo do mapa vira transparente, só as linhas ficam)
  const TINT = { cockpit: [196, 170, 255, 2.1], holograma: [103, 232, 249, 1.5], itinerario: [233, 225, 255, 1.6], radar: [110, 231, 150, 1.7],
    fusao1: [150, 220, 255, 1.6], fusao2: [125, 211, 252, 1.5], fusao3: [196, 181, 253, 1.7], fusao4: [165, 243, 252, 1.6] };
  const ICONS = {
    carro: 'M4 15V11l2-5h12l2 5v4M4 15h16M4 15v3h3v-3m10 0v3h3v-3M7.5 12.5h.01M16.5 12.5h.01',
    moto: 'M5 17a3 3 0 1 0 0-.01M19 17a3 3 0 1 0 0-.01M5 17l4-7h5l3 7M12 10l-1-3h-2M14 10h3l2 7',
    bike: 'M5.5 17.5a3.5 3.5 0 1 0 0-.01M18.5 17.5a3.5 3.5 0 1 0 0-.01M5.5 17.5L9 9h6l3.5 8.5M9 9l3 8.5 3-8.5M8 6h3',
    a_pe: 'M13 4a1.5 1.5 0 1 0 0 .01M10 21l2-6 3 3v3M12 15l-1-5 4 2 2 3M11 10l-3 2-1 3',
    onibus: 'M5 17V6a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v11M5 17h14M5 17v2h3v-2m8 0v2h3v-2M5 11h14M8 14h.01M16 14h.01',
  };

  const el = (tag, cls = '', text = '') => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  };
  const icon = (mode) => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('class', 'krv-ico');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', ICONS[mode] || ICONS.carro);
    svg.appendChild(path);
    return svg;
  };
  const shortPlace = (label) => String(label || '').split(',').slice(0, 2).join(',').trim();

  let leaflet = null;
  function loadLeaflet() {
    if (window.L) return Promise.resolve(window.L);
    if (leaflet) return leaflet;
    leaflet = new Promise((resolve, reject) => {
      const css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = LEAFLET_CSS;
      document.head.appendChild(css);
      const script = document.createElement('script');
      script.src = LEAFLET_JS;
      script.onload = () => resolve(window.L);
      script.onerror = () => reject(new Error('mapa indisponível'));
      document.head.appendChild(script);
    });
    return leaflet;
  }

  // ruas já recoloridas (por modelo e quadro): reabrir a rota ou trocar de modelo não refaz o trabalho pesado
  const painted = new Map();
  const PAINTED_MAX = 900;

  /** Camada de ruas recolorida: fundo do mapa cinza (~36) some, ruas (~70) viram linhas na cor do modelo. */
  function streetsLayer(L, model, tileUrl, maxNativeZoom) {
    const [r, g, b, k] = TINT[model];
    const Layer = L.GridLayer.extend({
      createTile(coords, done) {
        const canvas = document.createElement('canvas');
        canvas.width = canvas.height = 256;
        const key = `${model}|${coords.z}/${coords.x}/${coords.y}`;
        const cached = painted.get(key);
        if (cached) {
          canvas.getContext('2d').drawImage(cached, 0, 0);
          setTimeout(() => done(null, canvas), 0);
          return canvas;
        }
        const img = new Image();
        img.crossOrigin = 'anonymous';
        img.onload = () => {
          const ctx = canvas.getContext('2d', { willReadFrequently: true });
          ctx.drawImage(img, 0, 0);
          try {
            const data = ctx.getImageData(0, 0, 256, 256);
            const px = data.data;
            for (let i = 0; i < px.length; i += 4) {
              const alpha = Math.max(0, Math.min(255, (px[i] - 42) * k));
              px[i] = r; px[i + 1] = g; px[i + 2] = b; px[i + 3] = alpha;
            }
            ctx.putImageData(data, 0, 0);
            if (painted.size >= PAINTED_MAX) painted.delete(painted.keys().next().value);
            painted.set(key, canvas);
          } catch (_) { /* sem permissão de leitura: fica o mapa cinza mesmo */ }
          done(null, canvas);
        };
        img.onerror = () => done(null, canvas);
        img.src = tileUrl(coords.z, coords.x, coords.y);
        return canvas;
      },
    });
    return new Layer({ maxNativeZoom, maxZoom: 19, keepBuffer: 2, className: 'krv-streets' });
  }

  function mount(host, plan, { model = 'cockpit', tileUrl = null, maxNativeZoom = 16, intro = '' } = {}) {
    model = MODELS.includes(model) ? model : 'cockpit';
    const tiles = tileUrl || ((z, x, y) => STREETS.replace('{z}', z).replace('{y}', y).replace('{x}', x));
    const root = el('div', 'krv');
    root.dataset.model = model;
    const state = { mode: plan.mode, routeId: '', busIndex: 0, map: null, layers: [], timers: [] };
    const modes = Array.isArray(plan.modes) ? plan.modes : [];
    const routeById = (id) => (plan.routes || []).find((route) => route.id === id);

    // ---------- peças ----------
    const mapBox = el('div', 'krv-map');
    const zoom = el('div', 'krv-zoom');
    [['+', 'Aproximar', () => state.map?.zoomIn()], ['−', 'Afastar', () => state.map?.zoomOut()], ['⤢', 'Ver a rota inteira', () => fit()]].forEach(([t, label, fn]) => {
      const button = el('button', 'krv-zoom-btn', t);
      button.type = 'button';
      button.setAttribute('aria-label', label);
      button.addEventListener('click', fn);
      zoom.appendChild(button);
    });
    const close = el('button', 'krv-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', 'Fechar rota');
    close.addEventListener('click', () => api.onClose?.());

    const minutes = el('div', 'krv-minutes');
    const minutesNumber = el('b', 'krv-minutes-n');
    const minutesSub = el('span', 'krv-minutes-sub');
    minutes.append(minutesNumber, minutesSub);
    const places = el('div', 'krv-places');
    [['a', plan.origin], ['b', plan.destination]].forEach(([which, place]) => {
      const row = el('div', `krv-place is-${which}`);
      row.append(el('i', 'krv-pin'), el('span', 'krv-place-tag', which.toUpperCase()), el('span', 'krv-place-name', shortPlace(place?.label)));
      row.title = place?.label || '';
      places.appendChild(row);
    });
    const modeList = el('div', 'krv-modes');
    const maxSeconds = Math.max(...modes.map((m) => Math.min(m.seconds, 7200)), 1);
    const modeButtons = {};
    modes.forEach((m, index) => {
      const button = el('button', 'krv-mode');
      button.type = 'button';
      button.style.setProperty('--i', String(index));
      button.style.setProperty('--share', String(Math.min(m.seconds, 7200) / maxSeconds));
      button.dataset.mode = m.mode;
      const text = el('span', 'krv-mode-text');
      text.append(el('span', 'krv-mode-name', MODE_NAME[m.mode] || m.mode), el('b', 'krv-mode-time', m.duration));
      const extra = m.mode === 'onibus' ? (m.lines || []).join(' + ') : (m.estimate ? 'estimado' : m.distance);
      text.appendChild(el('small', 'krv-mode-extra', extra || ''));
      button.append(icon(m.mode), text, el('i', 'krv-mode-bar'));
      button.addEventListener('click', () => select(m.mode));
      modeButtons[m.mode] = button;
      modeList.appendChild(button);
    });
    const alts = el('div', 'krv-alts');
    const bus = el('div', 'krv-bus');
    const timeline = el('div', 'krv-timeline');

    // ---------- layout de cada modelo ----------
    if (model === 'cockpit' || model.startsWith('fusao')) {
      const side = el('div', 'krv-side');
      side.append(minutes, places, el('div', 'krv-label', 'Como ir'), modeList, alts, bus);
      const mapWrap = el('div', 'krv-map-wrap');
      mapWrap.append(mapBox, zoom);
      root.append(el('i', 'krv-unfold-line'), el('i', 'krv-holo-beam'), mapWrap, side, close);
    } else if (model === 'holograma') {
      const left = el('div', 'krv-holo-left');
      left.append(minutes);
      const right = el('div', 'krv-holo-right');
      right.append(places, alts);
      root.append(mapBox, el('i', 'krv-holo-beam'), left, right, modeList, bus, zoom, close);
    } else if (model === 'itinerario') {
      const head = el('div', 'krv-head');
      head.append(minutes, timeline);
      const mapWrap = el('div', 'krv-map-wrap');
      mapWrap.append(mapBox, zoom, alts);
      root.append(head, mapWrap, modeList, bus, close);
    } else {
      const lens = el('div', 'krv-lens');
      const ring = el('div', 'krv-ring');
      ring.append(el('i', 'krv-ring-a'), el('i', 'krv-ring-b'), el('i', 'krv-sweep'));
      lens.append(mapBox, ring);
      root.append(lens, minutes, places, modeList, alts, bus, zoom, close);
    }
    host.appendChild(root);

    // ---------- textos que mudam com o modo ----------
    function paintText() {
      const m = modes.find((item) => item.mode === state.mode) || modes[0] || {};
      const route = routeById(state.routeId);
      const busOption = state.mode === 'onibus' ? (plan.bus || [])[state.busIndex] : null;
      const duration = busOption ? busOption.duration : route ? route.duration : m.duration;
      const seconds = busOption ? busOption.seconds : route ? route.seconds : m.seconds;
      minutesNumber.dataset.value = String(duration || '');
      minutesNumber.textContent = duration || '';
      const arrive = new Date(Date.now() + (seconds || 0) * 1000);
      const hh = `${String(arrive.getHours()).padStart(2, '0')}:${String(arrive.getMinutes()).padStart(2, '0')}`;
      minutesSub.textContent = busOption ? `sai ${busOption.leave} · ${busOption.walk} a pé` : `${route?.distance || m.distance || ''} · chega ${hh} · ${(MODE_NAME[state.mode] || '').toLowerCase()}${m.estimate ? ' (estimado)' : ''}`;
      Object.entries(modeButtons).forEach(([key, button]) => button.classList.toggle('is-on', key === state.mode));
      // caminhos alternativos (carro/moto)
      alts.replaceChildren();
      const carRoutes = (plan.routes || []).filter((r) => r.mode === 'carro');
      if ((state.mode === 'carro' || state.mode === 'moto') && carRoutes.length > 1) {
        alts.appendChild(el('span', 'krv-label', 'Caminhos'));
        carRoutes.forEach((r, index) => {
          const button = el('button', `krv-alt${r.id === state.routeId ? ' is-on' : ''}`);
          button.type = 'button';
          button.append(el('b', '', `Rota ${index + 1}`), el('span', '', `${r.duration} · ${r.distance}`));
          button.addEventListener('click', () => { state.routeId = r.id; paintText(); drawRoute(true); });
          alts.appendChild(button);
        });
      }
      // ônibus: as linhas
      bus.replaceChildren();
      bus.hidden = state.mode !== 'onibus';
      if (state.mode === 'onibus') {
        bus.appendChild(el('span', 'krv-label', (plan.bus || []).length ? 'Ônibus' : 'Ônibus · sem linha nos dados abertos'));
        (plan.bus || []).forEach((option, index) => {
          const card = el('button', `krv-bus-opt${index === state.busIndex ? ' is-on' : ''}`);
          card.type = 'button';
          const badges = el('span', 'krv-bus-lines');
          option.lines.forEach((line) => badges.appendChild(el('b', 'krv-bus-line', line.line)));
          const first = option.lines[0] || {};
          card.append(badges, el('span', 'krv-bus-name', first.name || first.headsign || ''),
            el('small', 'krv-bus-meta', `${option.duration} · embarca ${first.from} · ${first.depart}${option.transfers ? ` · ${option.transfers} troca` : ''}`));
          card.addEventListener('click', () => { state.busIndex = index; paintText(); drawRoute(true); });
          bus.appendChild(card);
        });
      }
      // linha do tempo (itinerário): A — modo — B
      timeline.replaceChildren();
      const step = (cls, text) => { const s = el('span', `krv-tl-step ${cls}`); s.append(el('i'), el('span', '', text)); return s; };
      timeline.append(step('is-a', shortPlace(plan.origin?.label)));
      const mid = el('span', 'krv-tl-mid');
      mid.append(icon(state.mode), el('span', '', `${duration}`));
      timeline.append(mid, step('is-b', shortPlace(plan.destination?.label)));
    }

    // ---------- mapa ----------
    function fit(animate = true) {
      const all = state.layers.filter((layer) => layer.getBounds);
      if (!state.map || !all.length) return;
      const bounds = all[0].getBounds();
      all.slice(1).forEach((layer) => bounds.extend(layer.getBounds()));
      // cada modelo deixa espaço para o que fica por cima do mapa (minutos, lugares, botões, borda da lente)
      const room = { cockpit: [[60, 40], [40, 40]], holograma: [[300, 70], [330, 130]], itinerario: [[40, 40], [60, 40]], radar: [[120, 120], [120, 120]],
        fusao1: [[90, 80], [90, 80]], fusao2: [[150, 120], [150, 120]], fusao3: [[150, 120], [150, 120]], fusao4: [[190, 140], [150, 140]] }[model] || [[60, 50], [50, 50]];
      state.map.fitBounds(bounds, { paddingTopLeft: room[0], paddingBottomRight: room[1], animate });
    }

    /**
     * Desenha a rota escolhida. animate: a rota "acende" no estilo do modelo; how 'energy': uma esfera de energia sai
     * do A e desenha o caminho até o B em ms (entradas de routeIntros.js).
     */
    function drawRoute(animate, how = 'line', ms = 2200) {
      const L = window.L;
      if (!state.map || !L) return;
      state.layers.forEach((layer) => layer.remove());
      state.layers = [];
      const add = (latlngs, cls, extra = {}) => {
        const line = L.polyline(latlngs, { className: cls, interactive: Boolean(extra.onClick), smoothFactor: 1.2, ...extra.style }).addTo(state.map);
        if (extra.onClick) line.on('click', extra.onClick);
        state.layers.push(line);
        return line;
      };
      const mains = [];
      if (state.mode === 'onibus') {
        const option = (plan.bus || [])[state.busIndex];
        (option?.legs || []).forEach((leg) => {
          if (leg.path.length < 2) return;
          add(leg.path, `krv-glow ${leg.mode === 'onibus' ? 'is-bus' : 'is-walk'}`);
          mains.push(add(leg.path, `krv-line ${leg.mode === 'onibus' ? 'is-bus' : 'is-walk'}`));
        });
      } else {
        const m = modes.find((item) => item.mode === state.mode);
        if (!state.routeId || !routeById(state.routeId) || (routeById(state.routeId).mode !== 'carro' && (state.mode === 'carro' || state.mode === 'moto'))) state.routeId = m?.routeId || '';
        const chosen = routeById(state.routeId);
        if (state.mode === 'carro' || state.mode === 'moto') {
          (plan.routes || []).filter((r) => r.mode === 'carro' && r.id !== state.routeId).forEach((r) => {
            add(r.path, 'krv-alt-line', { onClick: () => { state.routeId = r.id; paintText(); drawRoute(true); } });
          });
        }
        if (chosen) {
          add(chosen.path, 'krv-glow');
          mains.push(add(chosen.path, 'krv-line'));
        }
      }
      const pin = (place, which) => {
        if (!place) return;
        const marker = L.marker([place.lat, place.lon], {
          icon: L.divIcon({ className: `krv-marker is-${which}`, html: `<i></i><span>${which.toUpperCase()}</span>`, iconSize: [30, 30], iconAnchor: [15, 15] }),
          keyboard: false, interactive: false,
        }).addTo(state.map);
        state.layers.push(marker);
      };
      pin(plan.origin, 'a');
      pin(plan.destination, 'b');
      if (animate) root.classList.remove('is-route-in', 'route-pending');
      // enquadra sem animar: o comprimento do traço é medido já no zoom final (senão a linha parava antes do B)
      fit(false);
      if (animate && how === 'energy') {
        requestAnimationFrame(() => runEnergy(mains, ms));
      } else if (animate) {
        // o traço da rota cresce de A até B (comprimento do SVG), no tempo e no jeito de cada modelo
        requestAnimationFrame(() => {
          mains.forEach((line) => {
            const path = line.getElement?.();
            if (!path?.getTotalLength) return;
            const length = Math.ceil(path.getTotalLength()) + 2;
            path.style.setProperty('--len', String(length));
          });
          void root.offsetWidth;
          root.classList.add('is-route-in');
          // terminado o desenho, a linha fica inteira (zoom depois não corta o traço)
          clearTimeout(state.drawn);
          state.drawn = setTimeout(() => root.classList.remove('is-route-in'), 2600);
        });
      }
    }

    /** Esfera elétrica percorrendo a rota, com o traço crescendo atrás dela (mesmo espaço do SVG do mapa). */
    function runEnergy(lines, ms) {
      const paths = lines.map((line) => line.getElement?.()).filter((path) => path?.getTotalLength);
      const glows = [...root.querySelectorAll('.krv-glow')];
      glows.forEach((g) => { g.style.transition = 'none'; g.style.strokeOpacity = '0'; });
      const items = paths.map((path) => {
        const len = path.getTotalLength();
        path.style.strokeDasharray = `${len} ${len}`;
        path.style.strokeDashoffset = String(len);
        // brilho da esfera: um círculo largo e transparente por trás (filtro de sombra custava um quadro inteiro)
        const halo = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        halo.setAttribute('r', '20');
        halo.setAttribute('class', 'krv-orb-halo');
        const orb = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        orb.setAttribute('r', '8');
        orb.setAttribute('class', 'krv-orb');
        path.parentNode.append(halo, orb);
        // pontos do caminho calculados uma vez (getPointAtLength a cada quadro custava ~1/3 do quadro sem placa de vídeo)
        const samples = Array.from({ length: 241 }, (_, i) => path.getPointAtLength((len * i) / 240));
        return { path, len, orb, halo, samples };
      });
      const total = items.reduce((sum, item) => sum + item.len, 0) || 1;
      const start = performance.now();
      cancelAnimationFrame(state.energy);
      const step = (now) => {
        const t = Math.min(1, (now - start) / ms);
        const eased = t < 0.5 ? 2 * t * t : 1 - ((-2 * t + 2) ** 2) / 2;
        let left = eased * total;
        items.forEach((item) => {
          const done = Math.max(0, Math.min(item.len, left));
          left -= item.len;
          item.path.style.strokeDashoffset = String(item.len - done);
          const at = (done / item.len) * 240;
          const a = item.samples[Math.floor(at)] || item.samples[240];
          const b = item.samples[Math.min(240, Math.floor(at) + 1)];
          const k = at - Math.floor(at);
          const point = { x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k };
          const on = done > 0 && done < item.len ? '1' : '0';
          [item.orb, item.halo].forEach((dot) => { dot.setAttribute('cx', String(point.x)); dot.setAttribute('cy', String(point.y)); dot.style.opacity = on; });
        });
        if (t < 1) { state.energy = requestAnimationFrame(step); return; }
        items.forEach((item) => { item.orb.remove(); item.halo.remove(); item.path.style.strokeDasharray = ''; item.path.style.strokeDashoffset = ''; });
        glows.forEach((g) => { g.style.transition = 'stroke-opacity .8s ease'; g.style.strokeOpacity = ''; });
        root.classList.add('is-energy-done');
      };
      root.classList.remove('is-energy-done');
      state.energy = requestAnimationFrame(step);
    }

    /** Pontos da rota em pixels da caixa da rota (para as partículas e a rede neural das entradas). */
    function routePoints(count = 60) {
      const chosen = routeById(state.routeId) || (plan.routes || [])[0];
      if (!state.map || !chosen) return [];
      const box = root.getBoundingClientRect();
      const mapBoxRect = mapBox.getBoundingClientRect();
      const path = chosen.path;
      return Array.from({ length: count }, (_, i) => {
        const p = state.map.latLngToContainerPoint(path[Math.floor((i / Math.max(1, count - 1)) * (path.length - 1))]);
        return { x: p.x + mapBoxRect.left - box.left, y: p.y + mapBoxRect.top - box.top };
      });
    }

    function select(mode) {
      if (!modes.some((m) => m.mode === mode)) return;
      state.mode = mode;
      state.routeId = '';
      state.busIndex = 0;
      paintText();
      drawRoute(true);
    }

    const api = {
      root,
      onClose: null,
      async ready() {
        const L = await loadLeaflet();
        state.map = L.map(mapBox, { zoomControl: false, attributionControl: true, zoomSnap: 0.25, wheelPxPerZoomLevel: 90, minZoom: 3, maxZoom: 19 });
        state.map.attributionControl.setPrefix('');
        state.map.attributionControl.addAttribution('Ruas © Esri · Rotas OSRM/OpenStreetMap · Ônibus Transitous');
        const streets = streetsLayer(L, model, tiles, maxNativeZoom).addTo(state.map);
        // a entrada só começa com as ruas prontas: num PC lento, recolorir os quadros travava a tela e a animação
        // passava sem ser vista (sobrava o mapa aparecendo do nada)
        const loaded = new Promise((resolve) => { streets.once('load', resolve); setTimeout(resolve, 6000); });
        state.map.setView([plan.destination.lat, plan.destination.lon], 13);
        paintText();
        drawRoute(false);
        await loaded;
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        return api;
      },
      /** Entrada do painel (e a rota logo depois). Com intro, a entrada escolhida (routeIntros.js) no lugar da do modelo. */
      playPanel() {
        const runIntro = intro && window.KiraRouteIntros?.[intro];
        state.timers.forEach(clearTimeout);
        cancelAnimationFrame(state.energy);
        if (runIntro) {
          // durante a entrada o orbe 3D da Kira pausa: o processador fica todo para a animação (volta sozinho)
          const orb = window.kira3D || window.__kira3dInstance;
          orb?.hold?.('rota', true);
          state.timers.push(setTimeout(() => orb?.hold?.('rota', false), 6000));
          root.classList.remove('is-in', 'is-route-in', 'is-energy-done');
          root.classList.add('is-intro', 'route-pending');
          state.map?.invalidateSize();
          fit(false);
          runIntro({
            root, mapBox, mapWrap: mapBox.parentElement === root ? mapBox : mapBox.closest('.krv-map-wrap, .krv-lens') || mapBox,
            drawRoute: (how = 'line', ms = 2200) => drawRoute(true, how, ms), routePoints, later: (fn, ms) => state.timers.push(setTimeout(fn, ms)),
          });
          return;
        }
        root.classList.remove('is-intro');
        root.classList.remove('is-in', 'is-route-in');
        void root.offsetWidth;
        root.classList.add('is-in');
        state.map?.invalidateSize();
        state.timers = [setTimeout(() => { state.map?.invalidateSize(); drawRoute(true); }, { cockpit: 650, holograma: 900, itinerario: 600, radar: 950 }[model] || 850)];
      },
      playRoute() { drawRoute(true); },
      setIntro(name) { intro = name || ''; },
      select,
      destroy() {
        state.timers.forEach(clearTimeout);
        (window.kira3D || window.__kira3dInstance)?.hold?.('rota', false);
        cancelAnimationFrame(state.energy);
        root.querySelectorAll('.krv-fx').forEach((node) => node.remove());
        state.map?.remove();
        root.remove();
      },
    };
    return api;
  }

  // ---------- rota flutuando por cima da tela ----------
  let float = null;
  let view = null;
  let hideTimer = 0;
  function hide() {
    if (!float || float.hidden) return { ok: true };
    clearTimeout(hideTimer);
    float.classList.add('is-out');
    setTimeout(() => { if (float.classList.contains('is-out')) { view?.destroy(); view = null; float.hidden = true; float.classList.remove('is-out'); } }, 420);
    return { ok: true };
  }
  async function show(arg = {}) {
    let plan = arg.plan;
    if (!plan && /^[0-9a-f]{12}$/.test(String(arg.id || ''))) {
      try { plan = await (await fetch(`/api/route/data/${arg.id}`, { cache: 'no-store' })).json(); } catch (_) { plan = null; }
    }
    if (!plan?.ok) return { ok: false, error: 'rota_indisponivel' };
    if (!float) {
      float = el('div', 'krv-float');
      document.body.appendChild(float);
      document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && float && !float.hidden) hide(); });
    }
    const model = MODELS.includes(arg.model) ? arg.model : 'cockpit';
    const key = `${plan.id}|${model}|${arg.mode || ''}`;
    // mesma rota e mesmo modelo já na tela: só toca a entrada de novo (sem remontar o mapa)
    if (view && view.key === key && !float.hidden && !float.classList.contains('is-out')) {
      view.setIntro(arg.intro || '');
      view.playPanel();
      clearTimeout(hideTimer);
      hideTimer = setTimeout(hide, Number(arg.hideAfterMs) || AUTO_HIDE_MS);
      return { ok: true };
    }
    view?.destroy();
    float.hidden = false;
    float.classList.remove('is-out');
    float.dataset.model = model;
    if (arg.mode) plan = { ...plan, mode: arg.mode };
    view = mount(float, plan, { model, tileUrl: arg.tileUrl, maxNativeZoom: arg.maxNativeZoom || 16, intro: arg.intro || '' });
    view.onClose = hide;
    view.key = key;
    await view.ready();
    view.playPanel();
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, Number(arg.hideAfterMs) || AUTO_HIDE_MS);
    return { ok: true };
  }

  window.KiraRouteView = { mount, MODELS };
  window.KiraRouteOverlay = {
    show(arg) { show(arg); return { ok: true }; },
    hide,
    replay(what) { if (!view) return; if (what === 'route') view.playRoute(); else view.playPanel(); },
    select(mode) { view?.select(mode); },
  };
})();
