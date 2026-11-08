// Olho de Deus de perto: quando o globo chega no limite do zoom, troca (com fade) por um mapa de satélite real (Esri
// World Imagery, até ruas e casas) com aviões ao vivo, terremotos e câmeras públicas. Leaflet: leve, sem WebGL.
// Afastar até o nível de continente volta para o globo no mesmo ponto.

const LEAFLET_JS = 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js';
const LEAFLET_CSS = 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css';
const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services';
const ATTRIBUTION = 'Imagens © Esri, Maxar, Earthstar Geographics · Aviões adsb.lol · Terremotos USGS';
export const ENTER_ZOOM = 5; // o globo vira mapa neste zoom
export const EXIT_ZOOM = 3; // abaixo disto o mapa volta a ser globo
const PLANES_EVERY_MS = 15000;
const FIRST_TILES_WAIT_MS = 1500; // o mapa só aparece com a foto carregada (ou depois disso, para não travar)
const DIVE_SECONDS = 2.4; // "mostra as ruas": mergulho do alto até o zoom pedido
const UNDERLAY_MAX_ZOOM = 9; // foto de baixa resolução por baixo: área ainda sem foto fica borrada, não vazia
const D2R = Math.PI / 180;
const RAIN_MAX_ZOOM = 7; // o RainViewer só tem quadros até o zoom 7; acima disso amplia
const EVENT_HEX = { wildfires: '#ff5a1f', severeStorms: '#38bdf8', volcanoes: '#ef4444', seaLakeIce: '#e0f2fe' };

/** Raio (km) que cobre a área visível, no limite que a API de aviões aceita (20-460). */
export function visibleRadiusKm(bounds) {
  const lat1 = bounds.south * D2R;
  const lat2 = bounds.north * D2R;
  const dLat = lat2 - lat1;
  const dLon = (bounds.east - bounds.west) * D2R;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  const diagonal = 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(a)));
  return Math.max(20, Math.min(460, Math.round(diagonal / 2)));
}

function el(doc, tag, className = '', text = '') {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function loadLeaflet(doc, win) {
  if (win.L) return Promise.resolve(win.L);
  return new Promise((resolve, reject) => {
    const css = doc.createElement('link');
    css.rel = 'stylesheet';
    css.href = LEAFLET_CSS;
    doc.head.appendChild(css);
    const script = doc.createElement('script');
    script.src = LEAFLET_JS;
    script.onload = () => resolve(win.L);
    script.onerror = () => reject(new Error('mapa indisponível'));
    doc.head.appendChild(script);
  });
}

/** Texto em várias linhas para popup, sempre como texto (nada de HTML vindo de fora). */
function card(doc, lines, imageUrl = '') {
  const box = el(doc, 'div', 'gods-eye-map-card');
  lines.filter(Boolean).forEach((line, index) => box.appendChild(el(doc, index ? 'div' : 'strong', '', line)));
  if (/^https:\/\//.test(imageUrl)) {
    const img = doc.createElement('img');
    img.src = imageUrl;
    img.alt = lines[0] || '';
    img.loading = 'lazy';
    box.appendChild(img);
  }
  return box;
}

export function createGodsEyeMap({ container, documentRef = globalThis.document, windowRef = globalThis.window, fetchJson, onExit, onInfo, quakeFilter = () => true }) {
  const doc = documentRef;
  const win = windowRef;
  let L = null;
  let map = null;
  let planesTimer = 0;
  let moveTimer = 0;
  let visible = false;
  // Cada abertura recebe uma geração. Respostas de rede e eventos Leaflet que
  // chegam depois de hide/destroy não podem tocar num mapa já removido.
  let lifecycle = 0;
  let photo = null;
  let pin = null;
  let rainLayer = null;
  const groups = {};
  const wanted = new Set();

  const planeIcon = (track) => L.divIcon({
    className: 'gods-eye-plane',
    html: `<span style="transform: rotate(${Number(track) || 0}deg)">✈</span>`,
    iconSize: [22, 22],
  });

  const mapIsAlive = (generation = lifecycle) => Boolean(visible && map && generation === lifecycle);

  async function drawPlanes(generation = lifecycle) {
    if (!mapIsAlive(generation) || !wanted.has('avioes')) return;
    const center = map.getCenter();
    const b = map.getBounds();
    const radius = visibleRadiusKm({ north: b.getNorth(), south: b.getSouth(), east: b.getEast(), west: b.getWest() });
    const items = (await fetchJson(`/api/godseye/planes?lat=${center.lat.toFixed(4)}&lon=${center.lng.toFixed(4)}&radius_km=${radius}`)).items || [];
    if (!mapIsAlive(generation)) return;
    groups.avioes.clearLayers();
    for (const plane of items) {
      L.marker([plane.lat, plane.lon], { icon: planeIcon(plane.track) })
        .bindPopup(card(doc, [`Voo ${plane.callsign || '?'}`, `${plane.type || 'aeronave'} · ${Number(plane.alt_ft || 0).toLocaleString('pt-BR')} pés`, `${plane.speed_kt} nós · rumo ${plane.track}°`]))
        .addTo(groups.avioes);
    }
    onInfo?.('avioes', items.length);
  }

  async function drawQuakes(generation = lifecycle) {
    if (!mapIsAlive(generation) || !wanted.has('terremotos')) return;
    const items = ((await fetchJson('/api/godseye/earthquakes')).items || []).filter(quakeFilter);
    if (!mapIsAlive(generation)) return;
    groups.terremotos.clearLayers();
    for (const quake of items) {
      L.circleMarker([quake.lat, quake.lon], { radius: 4 + quake.mag * 2, color: quake.mag >= 5.5 ? '#ef4444' : quake.mag >= 4 ? '#f97316' : '#fbbf24', weight: 2, fillOpacity: 0.35 })
        .bindPopup(card(doc, [`Terremoto M${quake.mag}`, quake.place]))
        .addTo(groups.terremotos);
    }
    onInfo?.('terremotos', items.length);
  }

  async function drawCameras(generation = lifecycle) {
    if (!mapIsAlive(generation) || !wanted.has('cameras')) return;
    const center = map.getCenter();
    const answer = await fetchJson(`/api/godseye/webcams?lat=${center.lat.toFixed(4)}&lon=${center.lng.toFixed(4)}&radius_km=50`);
    if (!mapIsAlive(generation)) return;
    groups.cameras.clearLayers();
    for (const cam of answer.items || []) {
      L.circleMarker([cam.lat, cam.lon], { radius: 6, color: '#22d3ee', weight: 2, fillOpacity: 0.6 })
        .bindPopup(card(doc, [cam.title, cam.city, cam.url ? 'Ao vivo no site do Windy' : ''], cam.image))
        .addTo(groups.cameras);
    }
    onInfo?.('cameras', (answer.items || []).length, answer.needs_key);
  }

  async function drawRain(generation = lifecycle) {
    if (!mapIsAlive(generation) || !wanted.has('chuva')) {
      rainLayer?.remove();
      rainLayer = null;
      return;
    }
    const rain = (await fetchJson('/api/godseye/rain')).items;
    if (!mapIsAlive(generation)) return;
    if (!rain?.host) return;
    const url = `${rain.host}${rain.path}/256/{z}/{x}/{y}/2/1_1.png`;
    if (rainLayer?.options.kiraUrl === url) return;
    rainLayer?.remove();
    rainLayer = L.tileLayer(url, { maxNativeZoom: RAIN_MAX_ZOOM, maxZoom: 19, opacity: 0.7, kiraUrl: url, attribution: 'Chuva RainViewer' }).addTo(map);
  }

  async function drawLightning(generation = lifecycle) {
    if (!mapIsAlive(generation) || !wanted.has('raios')) return;
    const items = (await fetchJson('/api/godseye/lightning')).items || [];
    if (!mapIsAlive(generation)) return;
    const cut = Date.now() / 1000 - 120;
    groups.raios.clearLayers();
    for (const strike of items) {
      L.circleMarker([strike.lat, strike.lon], { radius: strike.t >= cut ? 5 : 3, color: strike.t >= cut ? '#fffbeb' : '#facc15', weight: 1, fillOpacity: 0.8 })
        .addTo(groups.raios);
    }
    onInfo?.('raios', items.length);
  }

  async function drawEvents(generation = lifecycle) {
    if (!mapIsAlive(generation) || !wanted.has('eventos')) return;
    const items = (await fetchJson('/api/godseye/eonet')).items || [];
    if (!mapIsAlive(generation)) return;
    groups.eventos.clearLayers();
    for (const event of items) {
      L.circleMarker([event.lat, event.lon], { radius: 7, color: EVENT_HEX[event.cat] || '#4ade80', weight: 2, fillOpacity: 0.5 })
        .bindPopup(card(doc, [event.title, `${event.kind} · NASA EONET`, event.date]))
        .addTo(groups.eventos);
    }
    onInfo?.('eventos', items.length);
  }

  async function drawShips(generation = lifecycle) {
    if (!mapIsAlive(generation) || !wanted.has('navios')) return;
    const center = map.getCenter();
    const b = map.getBounds();
    const radius = visibleRadiusKm({ north: b.getNorth(), south: b.getSouth(), east: b.getEast(), west: b.getWest() });
    const answer = await fetchJson(`/api/godseye/ships?lat=${center.lat.toFixed(4)}&lon=${center.lng.toFixed(4)}&radius_km=${radius}`);
    if (!mapIsAlive(generation)) return;
    groups.navios.clearLayers();
    for (const ship of answer.items || []) {
      L.marker([ship.lat, ship.lon], { icon: L.divIcon({ className: 'gods-eye-ship', html: `<span style="transform: rotate(${Number(ship.course) || 0}deg)">▲</span>`, iconSize: [16, 16] }) })
        .bindPopup(card(doc, [ship.name || `Navio ${ship.mmsi}`, `${ship.speed_kt ?? 0} nós · rumo ${ship.course ?? '?'}°`, ship.dest ? `destino ${ship.dest}` : '']))
        .addTo(groups.navios);
    }
    onInfo?.('navios', (answer.items || []).length, answer.needs_key);
  }

  // bikes compartilhadas e rádios perto do centro do mapa (barra da direita > Cidade)
  async function drawSpots(generation = lifecycle) {
    if (!mapIsAlive(generation)) return;
    const c = map.getCenter();
    const at = `lat=${c.lat.toFixed(3)}&lon=${c.lng.toFixed(3)}`;
    if (wanted.has('bicicletas')) {
      const net = (await fetchJson(`/api/godseye/bikes?${at}`)).items;
      if (!mapIsAlive(generation)) return;
      groups.bicicletas.clearLayers();
      for (const b of net?.items || []) {
        L.circleMarker([b.lat, b.lon], { radius: 5, color: '#34d399', weight: 2, fillOpacity: b.free ? 0.8 : 0.15 })
          .bindPopup(card(doc, [b.name, `${b.free ?? '?'} bikes livres · ${b.slots ?? '?'} vagas`, net.name])).addTo(groups.bicicletas);
      }
      onInfo?.('bicicletas', (net?.items || []).length);
    }
    if (wanted.has('radios')) {
      const items = (await fetchJson(`/api/godseye/radios?${at}`)).items || [];
      if (!mapIsAlive(generation)) return;
      groups.radios.clearLayers();
      for (const r of items) L.circleMarker([r.lat, r.lon], { radius: 6, color: '#facc15', weight: 2, fillOpacity: 0.6 }).bindPopup(card(doc, [r.name, 'Rádio'])).addTo(groups.radios);
      onInfo?.('radios', items.length);
    }
  }

  function refresh(generation = lifecycle) {
    drawSpots(generation).catch(() => {});
    drawPlanes(generation).catch(() => {});
    drawCameras(generation).catch(() => {});
    drawShips(generation).catch(() => {});
  }

  function refreshSlow(generation = lifecycle) {
    drawRain(generation).catch(() => {});
    drawLightning(generation).catch(() => {});
    drawEvents(generation).catch(() => {});
  }

  const api = {
    get isVisible() { return visible; },
    async show(lat, lon, zoom = ENTER_ZOOM) {
      const generation = visible ? lifecycle : lifecycle + 1;
      lifecycle = generation;
      L = await loadLeaflet(doc, win);
      if (generation !== lifecycle) return { ok: false, cancelled: true };
      if (!map) {
        map = L.map(container, { zoomControl: false, worldCopyJump: true, minZoom: 2, maxZoom: 19, attributionControl: true });
        const mapInstance = map;
        map.attributionControl.setPrefix('');
        L.tileLayer(`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`, { maxNativeZoom: UNDERLAY_MAX_ZOOM, maxZoom: 19, keepBuffer: 1 }).addTo(map);
        // a foto da Esri vai até o zoom 17 em boa parte do Brasil; acima disso amplia a 17 (sem o aviso "Map data not yet available")
        photo = L.tileLayer(`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`, { maxNativeZoom: 17, maxZoom: 19, keepBuffer: 4, attribution: ATTRIBUTION }).addTo(map);
        L.tileLayer(`${ESRI}/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}`, { maxZoom: 19, opacity: 0.9 }).addTo(map);
        L.tileLayer(`${ESRI}/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}`, { maxZoom: 19, minZoom: 12, opacity: 0.8 }).addTo(map);
        for (const key of ['avioes', 'terremotos', 'cameras', 'raios', 'eventos', 'navios', 'bicicletas', 'radios']) groups[key] = L.layerGroup().addTo(map);
        map.on('zoomend', () => {
          if (map !== mapInstance || !visible || mapInstance.getZoom() >= EXIT_ZOOM) return;
          onExit?.(mapInstance.getCenter().lat, mapInstance.getCenter().lng);
        });
        map.on('moveend', () => {
          if (map !== mapInstance || !visible) return;
          win.clearTimeout(moveTimer);
          moveTimer = win.setTimeout(() => refresh(generation), 600);
        });
      }
      if (!map || generation !== lifecycle) return { ok: false, cancelled: true };
      const firstZoom = Math.min(zoom, ENTER_ZOOM);
      map.invalidateSize();
      const tilesReady = new Promise((resolve) => {
        photo.once('load', resolve);
        win.setTimeout(resolve, FIRST_TILES_WAIT_MS);
      });
      map.setView([lat, lon], firstZoom, { animate: false });
      if (!visible) await tilesReady;
      if (generation !== lifecycle || !map) return { ok: false, cancelled: true };
      visible = true;
      container.classList.add('is-visible');
      if (zoom > firstZoom) map.flyTo([lat, lon], zoom, { duration: DIVE_SECONDS });
      planesTimer = planesTimer || win.setInterval(() => {
        if (!mapIsAlive(generation)) return;
        drawPlanes(generation).catch(() => {});
        drawLightning(generation).catch(() => {});
        drawShips(generation).catch(() => {});
      }, PLANES_EVERY_MS);
      drawQuakes(generation).catch(() => {});
      refresh(generation);
      refreshSlow(generation);
      return { ok: true };
    },
    hide() {
      lifecycle += 1;
      visible = false;
      container.classList.remove('is-visible');
      win.clearInterval(planesTimer);
      planesTimer = 0;
      win.clearTimeout(moveTimer);
      moveTimer = 0;
      try { map?.stop?.(); } catch (_) { /* Leaflet pode já estar desmontado */ }
    },
    redrawQuakes() { drawQuakes(lifecycle).catch(() => {}); },
    setLayers(keys) {
      wanted.clear();
      keys.forEach((key) => wanted.add(key));
      if (!map) return;
      for (const key of Object.keys(groups)) if (!wanted.has(key)) groups[key].clearLayers();
      drawQuakes(lifecycle).catch(() => {});
      refresh(lifecycle);
      refreshSlow(lifecycle);
    },
    /** Pino no lugar pedido ("mostra a Rua Pomar Carioca"): o Chefe vê onde é, e a Kira pode dizer "no pino". */
    mark(lat, lon, label) {
      if (!map) return;
      pin?.remove();
      pin = L.marker([lat, lon], { icon: L.divIcon({ className: 'gods-eye-pin', html: '<span></span>', iconSize: [26, 26], iconAnchor: [13, 26] }), keyboard: false })
        .bindTooltip(el(doc, 'span', '', label || 'Aqui'), { permanent: true, direction: 'top', offset: [0, -24], className: 'gods-eye-pin-label' })
        .addTo(map);
    },
    /** Afasta o mapa até o nível de continente (início da viagem para outro lugar). */
    pullBack(seconds = 1.2) {
      if (!map || !visible) return Promise.resolve();
      const generation = lifecycle;
      const mapInstance = map;
      return new Promise((resolve) => {
        mapInstance.once('moveend', resolve);
        win.setTimeout(resolve, seconds * 1000 + 800);
        if (mapIsAlive(generation)) mapInstance.flyTo(mapInstance.getCenter(), EXIT_ZOOM, { duration: seconds });
      });
    },
    /** Já no mapa: aproxima no ponto sem recomeçar o mergulho do alto. */
    focusOn(lat, lon, zoom) {
      if (!map || !visible) return;
      map.flyTo([lat, lon], zoom, { duration: 1.2 });
    },
    center() { return map ? { lat: map.getCenter().lat, lon: map.getCenter().lng, zoom: map.getZoom() } : null; },
    zoomBy(delta) {
      if (!map || !visible) return;
      const amount = Number(delta);
      if (!Number.isFinite(amount)) return;
      map.setZoom(map.getZoom() + amount);
    },
    destroy() {
      api.hide();
      const oldMap = map;
      map = null;
      photo = null;
      pin = null;
      rainLayer = null;
      try { oldMap?.remove?.(); } catch (_) { /* já removido pelo Leaflet */ }
    },
  };
  return api;
}
