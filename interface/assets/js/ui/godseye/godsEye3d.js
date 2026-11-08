// Olho de Deus em 3D ("mostra em 3D"): prédios em volume (OpenFreeMap, sem chave) sobre a foto de satélite da Esri,
// câmera inclinada girando devagar em volta do lugar. MapLibre GL do cdnjs; com WebGL por software também roda.

const MAPLIBRE_JS = 'https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.js';
const MAPLIBRE_CSS = 'https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.css';
const PHOTO = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}';
const BUILDINGS = 'https://tiles.openfreemap.org/planet';
const ATTRIBUTION = 'Imagens © Esri, Maxar · Prédios © OpenStreetMap, OpenFreeMap';
const ORBIT_DEG_PER_SECOND = 5;
const DEFAULT_BUILDING_METERS = 6; // casa sem altura no OpenStreetMap: um ou dois andares

export const STYLE_3D = Object.freeze({
  version: 8,
  sources: {
    foto: { type: 'raster', tiles: [PHOTO], tileSize: 256, maxzoom: 17, attribution: ATTRIBUTION },
    osm: { type: 'vector', url: BUILDINGS },
  },
  layers: [
    { id: 'foto', type: 'raster', source: 'foto' },
    {
      id: 'predios',
      type: 'fill-extrusion',
      source: 'osm',
      'source-layer': 'building',
      minzoom: 14,
      paint: {
        'fill-extrusion-color': '#c4b5fd',
        'fill-extrusion-height': ['coalesce', ['get', 'render_height'], DEFAULT_BUILDING_METERS],
        'fill-extrusion-base': ['coalesce', ['get', 'render_min_height'], 0],
        'fill-extrusion-opacity': 0.6,
      },
    },
  ],
});

function loadMaplibre(doc, win) {
  if (win.maplibregl) return Promise.resolve(win.maplibregl);
  return new Promise((resolve, reject) => {
    const css = doc.createElement('link');
    css.rel = 'stylesheet';
    css.href = MAPLIBRE_CSS;
    doc.head.appendChild(css);
    const script = doc.createElement('script');
    script.src = MAPLIBRE_JS;
    script.onload = () => resolve(win.maplibregl);
    script.onerror = () => reject(new Error('3D indisponível'));
    doc.head.appendChild(script);
  });
}

export function createGodsEye3d({ container, documentRef = globalThis.document, windowRef = globalThis.window }) {
  const doc = documentRef;
  const win = windowRef;
  let map = null;
  let marker = null;
  let orbitFrame = 0;
  let visible = false;

  const stopOrbit = () => {
    if (orbitFrame) win.cancelAnimationFrame(orbitFrame);
    orbitFrame = 0;
  };

  const orbit = () => {
    let last = win.performance.now();
    const step = (now) => {
      map.setBearing(map.getBearing() + ((now - last) / 1000) * ORBIT_DEG_PER_SECOND);
      last = now;
      orbitFrame = win.requestAnimationFrame(step);
    };
    orbitFrame = win.requestAnimationFrame(step);
  };

  const api = {
    get isVisible() { return visible; },
    async show(lat, lon, label = '') {
      const maplibregl = await loadMaplibre(doc, win);
      if (!map) {
        map = new maplibregl.Map({
          container, style: STYLE_3D, center: [lon, lat], zoom: 17.2, pitch: 62, bearing: -30, maxPitch: 75,
          antialias: false, attributionControl: { compact: true },
        });
        // o Chefe mexeu: a câmera para de girar e fica onde ele deixou
        for (const event of ['mousedown', 'wheel', 'touchstart']) map.on(event, stopOrbit);
      } else {
        map.jumpTo({ center: [lon, lat], zoom: 17.2, pitch: 62, bearing: -30 });
      }
      marker?.remove();
      const pin = doc.createElement('div');
      pin.className = 'gods-eye-pin';
      pin.appendChild(doc.createElement('span'));
      pin.title = label;
      marker = new maplibregl.Marker({ element: pin, anchor: 'bottom' }).setLngLat([lon, lat]).addTo(map);
      visible = true;
      container.classList.add('is-visible');
      map.resize();
      stopOrbit();
      orbit();
      return { ok: true };
    },
    hide() {
      visible = false;
      stopOrbit();
      container.classList.remove('is-visible');
    },
    center() { return map ? { lat: map.getCenter().lat, lon: map.getCenter().lng } : null; },
    destroy() {
      api.hide();
      map?.remove();
      map = null;
    },
  };
  return api;
}
