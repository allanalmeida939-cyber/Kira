const DEFAULT_BOUNDS = Object.freeze({ width: 1100, height: 700 });
const MARGIN = 24;
const GAP = 14;

const EDGE_MARGIN = 40;
/** Painel nunca passa de 85% da altura visível; o resto do conteúdo rola dentro dele. */
export const MAX_HEIGHT_RATIO = 0.85;
export const VIEWPORT_MARGIN = 24;

export const MEDIA_PANEL_SPECS = Object.freeze({
  viewer: Object.freeze({ width: 760, height: 520, minWidth: 320, minHeight: 240 }),
  presentation: Object.freeze({ width: 720, height: 560, minWidth: 340, minHeight: 280 }),
  gallery: Object.freeze({ width: 640, height: 460, minWidth: 280, minHeight: 220 }),
  video: Object.freeze({ width: 560, height: 400, minWidth: 280, minHeight: 220 }),
  article: Object.freeze({ width: 520, height: 480, minWidth: 280, minHeight: 220 }),
  'search-log': Object.freeze({ width: 360, height: 260, minWidth: 280, minHeight: 200 }),
  scoreboard: Object.freeze({ width: 560, height: 420, minWidth: 300, minHeight: 220 }),
  election: Object.freeze({ width: 520, height: 560, minWidth: 320, minHeight: 260 }),
  info: Object.freeze({ width: 520, height: 420, minWidth: 300, minHeight: 220 }),
  tutor: Object.freeze({ width: 620, height: 640, minWidth: 360, minHeight: 300 }),
  quadro: Object.freeze({ width: 540, height: 520, minWidth: 300, minHeight: 220 }),
  download: Object.freeze({ width: 420, height: 220, minWidth: 280, minHeight: 180 }),
  // quase quadrado (a imagem + a barra do título); em 1366x705 o encolhimento de 70% cabe entre o relógio e os controles
  creation: Object.freeze({ width: 590, height: 618, minWidth: 320, minHeight: 320 }),
});

/** Tamanhos padrão dos painéis que não são mídia (nunca viram abas). */
export const PANEL_DEFAULT_SIZES = Object.freeze({
  reminders: Object.freeze({ width: 520, height: 560, minWidth: 320, minHeight: 300 }),
  smart_home: Object.freeze({ width: 900, height: 560, minWidth: 720, minHeight: 480 }), // Neon de LED: aparelhos + controle sem rolar
});

/** Escala o tamanho padrão proporcionalmente para caber na janela com margem de 40 px. */
export function fitSize(spec, bounds) {
  const maxWidth = Math.max(1, bounds.width - EDGE_MARGIN * 2);
  const maxHeight = Math.max(1, Math.min(bounds.height - EDGE_MARGIN * 2, Math.floor(bounds.height * MAX_HEIGHT_RATIO)));
  const factor = Math.min(1, maxWidth / spec.width, maxHeight / spec.height);
  return {
    width: Math.round(Math.min(maxWidth, Math.max(spec.width * factor, Math.min(spec.minWidth, maxWidth)))),
    height: Math.round(Math.min(maxHeight, Math.max(spec.height * factor, Math.min(spec.minHeight, maxHeight)))),
  };
}

function finite(value, fallback = 0) {
  const result = Number(value);
  return Number.isFinite(result) ? result : fallback;
}

function positive(value, fallback) {
  const result = finite(value, fallback);
  return result > 0 ? result : fallback;
}

function rect(value) {
  return {
    x: finite(value?.x ?? value?.left),
    y: finite(value?.y ?? value?.top),
    width: positive(value?.width, 0),
    height: positive(value?.height, 0),
  };
}

function overlapArea(left, right) {
  const width = Math.max(0, Math.min(left.x + left.width, right.x + right.width) - Math.max(left.x, right.x));
  const height = Math.max(0, Math.min(left.y + left.height, right.y + right.height) - Math.max(left.y, right.y));
  return width * height;
}

function fits(candidate, bounds) {
  return candidate.x >= 0 && candidate.y >= 0 && candidate.x + candidate.width <= bounds.width && candidate.y + candidate.height <= bounds.height;
}

function candidatePositions(bounds, size, occupied) {
  const positions = [];
  const maxX = bounds.width - size.width - MARGIN;
  const maxY = bounds.height - size.height - MARGIN;
  for (let y = MARGIN; y <= maxY; y += 16) {
    for (let x = MARGIN; x <= maxX; x += 16) positions.push({ x, y });
  }
  positions.push({ x: Math.round((bounds.width - size.width) / 2), y: Math.round((bounds.height - size.height) / 2) });
  occupied.forEach((zone) => {
    const centeredY = Math.round(zone.y + (zone.height - size.height) / 2);
    positions.push({ x: Math.round(zone.x + zone.width + GAP), y: centeredY }, { x: Math.round(zone.x - size.width - GAP), y: centeredY });
  });
  return positions;
}

/**
 * Escolhe onde abrir um painel: o primeiro fica no centro da tela, o seguinte ao lado, sem sobrepor
 * painéis abertos nem áreas proibidas (controles, relógio, menu). O orbe pode ficar atrás.
 */
export function choosePosition(size, bounds, forbidden, occupied) {
  const center = { x: bounds.width / 2, y: bounds.height / 2 };
  let best = null;
  let bestScore = Number.POSITIVE_INFINITY;
  for (const position of candidatePositions(bounds, size, occupied)) {
    const candidate = { ...position, width: size.width, height: size.height };
    if (!fits(candidate, bounds)) continue;
    const forbiddenArea = forbidden.reduce((total, zone) => total + overlapArea(candidate, zone), 0);
    const occupiedArea = occupied.reduce((total, zone) => total + overlapArea(candidate, zone), 0);
    const distance = Math.hypot(candidate.x + size.width / 2 - center.x, candidate.y + size.height / 2 - center.y);
    const score = forbiddenArea * 1000 + occupiedArea * 500 + distance;
    if (score < bestScore) { best = candidate; bestScore = score; }
  }
  return best || { x: Math.max(0, Math.round((bounds.width - size.width) / 2)), y: Math.max(0, Math.round((bounds.height - size.height) / 2)), width: size.width, height: size.height };
}

function isFree(candidate, forbidden, occupied) {
  return !forbidden.some((zone) => overlapArea(candidate, zone) > 0) && !occupied.some((zone) => overlapArea(candidate, zone) > 0);
}

function normalizeInput(input) {
  if (Array.isArray(input)) return { bounds: DEFAULT_BOUNDS, panels: input, forbiddenRects: [], occupiedRects: [] };
  const source = input && typeof input === 'object' ? input : {};
  const viewport = source.bounds || source.viewport || DEFAULT_BOUNDS;
  const bounds = { width: positive(viewport.width, DEFAULT_BOUNDS.width), height: positive(viewport.height, DEFAULT_BOUNDS.height) };
  const panels = Array.isArray(source.panels) ? source.panels : Array.isArray(source.rects) ? source.rects : [];
  const forbiddenRects = (source.forbiddenRects || source.forbidden || []).map(rect).filter((item) => item.width && item.height);
  const occupiedRects = (source.occupiedRects || source.occupied || []).map(rect).filter((item) => item.width && item.height);
  return { bounds, panels, forbiddenRects, occupiedRects };
}

/**
 * Produces media rectangles without reading or mutating DOM state. Existing user-touched
 * panels are returned unchanged; the caller decides whether to apply the other results.
 */
export function placeMediaPanels(input = {}) {
  const { bounds, panels, forbiddenRects, occupiedRects } = normalizeInput(input);
  const compact = bounds.width <= 800 && bounds.height <= 550;
  const known = panels.filter((panel) => MEDIA_PANEL_SPECS[panel?.kind] && panel?.id);

  const layout = (ignoreFixed, scale, shrinkEach) => {
    const placed = [];
    const occupied = [...occupiedRects];
    let free = true;
    for (const panel of known) {
      const spec = MEDIA_PANEL_SPECS[panel.kind];
      if (panel.userTouched || panel.pinned || (panel.fixed && !ignoreFixed)) {
        const kept = { id: panel.id, kind: panel.kind, x: finite(panel.x), y: finite(panel.y), width: positive(panel.width, spec.width), height: positive(panel.height, spec.height), tabbed: compact };
        placed.push(kept);
        occupied.push(kept);
        continue;
      }
      let size = fitSize({ ...spec, width: spec.width * scale, height: spec.height * scale }, bounds);
      let next = choosePosition(size, bounds, forbiddenRects, occupied);
      if (shrinkEach) {
        for (const factor of [0.9, 0.8, 0.7, 0.6]) {
          if (isFree(next, forbiddenRects, occupied)) break;
          size = fitSize({ ...spec, width: spec.width * factor, height: spec.height * factor }, bounds);
          next = choosePosition(size, bounds, forbiddenRects, occupied);
        }
      }
      free = free && isFree(next, forbiddenRects, occupied);
      const result = { id: panel.id, kind: panel.kind, x: Math.round(next.x), y: Math.round(next.y), width: Math.round(size.width), height: Math.round(size.height), tabbed: compact };
      placed.push(result);
      occupied.push(result);
    }
    return { placed, free };
  };

  const first = layout(false, 1, true);
  if (first.free) return first.placed;
  // Não coube sem sobrepor: reorganiza os painéis automáticos juntos, reduzindo-os proporcionalmente.
  let last = first;
  for (const scale of [1, 0.9, 0.8, 0.7, 0.6, 0.5]) {
    last = layout(true, scale, false);
    if (last.free) return last.placed;
  }
  return last.placed;
}

/** Tamanho e posição de um painel avulso: encolhe proporcionalmente até caber livre da área proibida. */
export function placeSingle(spec, bounds, forbidden, occupied) {
  let size = fitSize(spec, bounds);
  let spot = choosePosition(size, bounds, forbidden, occupied);
  for (const factor of [0.9, 0.8, 0.7, 0.6]) {
    if (isFree(spot, forbidden, occupied)) break;
    size = fitSize({ ...spec, width: spec.width * factor, height: spec.height * factor }, bounds);
    spot = choosePosition(size, bounds, forbidden, occupied);
  }
  return { x: spot.x, y: spot.y, width: size.width, height: size.height };
}

/**
 * Encaixa um retângulo na área visível: margem de 24 px, altura máxima de 85% e tamanho mínimo respeitado
 * só enquanto couber. Usado ao abrir, depois do layout assentar e no resize, para painéis que o usuário não moveu.
 */
export function clampToViewport(value, viewport, { margin = VIEWPORT_MARGIN, minWidth = 120, minHeight = 80 } = {}) {
  const box = rect(value);
  const availableWidth = Math.max(1, finite(viewport?.width) - margin * 2);
  const availableHeight = Math.max(1, Math.min(finite(viewport?.height) - margin * 2, Math.floor(finite(viewport?.height) * MAX_HEIGHT_RATIO)));
  const width = Math.round(Math.min(availableWidth, Math.max(box.width, Math.min(minWidth, availableWidth))));
  const height = Math.round(Math.min(availableHeight, Math.max(box.height, Math.min(minHeight, availableHeight))));
  const x = Math.round(Math.min(Math.max(box.x, margin), Math.max(margin, finite(viewport?.width) - margin - width)));
  const y = Math.round(Math.min(Math.max(box.y, margin), Math.max(margin, finite(viewport?.height) - margin - height)));
  return { x, y, width, height };
}

/** O que a Kira mostrou antes vira miniatura de até metade do tamanho, na coluna do lado (como no briefing). */
export const MINI_MAX_SCALE = 0.5;
export const MAX_MINIS = 2;
const STAGE_SIDE_RATIO = 0.3;
const MIN_STAGE = Object.freeze({ width: 360, height: 240 });

/**
 * Área livre do meio da tela, onde a Kira encena os painéis: entre as colunas laterais (transcrição, APIs, botões)
 * e entre o relógio e os controles. Tela sem nada nos lados (janela 2) usa a janela toda.
 */
export function freeStageArea(bounds, forbidden = [], margin = MARGIN) {
  const width = positive(bounds?.width, DEFAULT_BOUNDS.width);
  const height = positive(bounds?.height, DEFAULT_BOUNDS.height);
  const zones = (forbidden || []).map(rect).filter((zone) => zone.width && zone.height);
  const middleTop = height * 0.3;
  const middleBottom = height * 0.7;
  const crossesMiddle = (zone) => zone.y < middleBottom && zone.y + zone.height > middleTop;
  let left = 0;
  let right = width;
  zones.filter(crossesMiddle).forEach((zone) => {
    if (zone.x + zone.width / 2 < width / 2) left = Math.max(left, zone.x + zone.width);
    else right = Math.min(right, zone.x);
  });
  let top = 0;
  let bottom = height;
  zones.filter((zone) => !crossesMiddle(zone) && zone.x < right && zone.x + zone.width > left).forEach((zone) => {
    if (zone.y + zone.height / 2 < height / 2) top = Math.max(top, zone.y + zone.height);
    else bottom = Math.min(bottom, zone.y);
  });
  const area = { x: left + margin, y: top + margin, width: right - left - margin * 2, height: bottom - top - margin * 2 };
  if (area.width < MIN_STAGE.width || area.height < MIN_STAGE.height) {
    return { x: margin, y: margin, width: Math.max(1, width - margin * 2), height: Math.max(1, height - margin * 2) };
  }
  return area;
}

/**
 * Palco das janelas da Kira: o painel mais novo em destaque (tamanho real, cabendo na coluna principal) e os
 * anteriores como miniaturas (escala) empilhadas na coluna da esquerda. Nada sobrepõe e nada sai da área.
 * @returns {{featured: {id:string,x:number,y:number,width:number,height:number}, minis: {id:string,x:number,y:number,scale:number}[]}}
 */
export function stagePanels({ area, featured, minis = [], gap = GAP }) {
  const list = (minis || []).filter((mini) => mini && mini.id).slice(0, MAX_MINIS);
  const sideWidth = list.length ? Math.round(area.width * STAGE_SIDE_RATIO) : 0;
  const offset = sideWidth ? sideWidth + gap : 0;
  const mainWidth = Math.max(1, area.width - offset);
  const spec = { width: positive(featured?.width, 480), height: positive(featured?.height, 320), minWidth: finite(featured?.minWidth), minHeight: finite(featured?.minHeight) };
  const factor = Math.min(1, mainWidth / spec.width, area.height / spec.height);
  const width = Math.round(Math.min(mainWidth, Math.max(spec.width * factor, Math.min(spec.minWidth, mainWidth))));
  const height = Math.round(Math.min(area.height, Math.max(spec.height * factor, Math.min(spec.minHeight, area.height))));
  const main = { id: featured?.id, x: Math.round(area.x + offset + (mainWidth - width) / 2), y: Math.round(area.y), width, height };
  let scales = list.map((mini) => Math.min(MINI_MAX_SCALE, sideWidth / positive(mini.width, sideWidth)));
  const gaps = gap * Math.max(0, list.length - 1);
  const stacked = list.reduce((sum, mini, index) => sum + positive(mini.height, 1) * scales[index], 0);
  if (stacked + gaps > area.height && stacked > 0) scales = scales.map((scale) => scale * Math.max(0.05, (area.height - gaps) / stacked));
  let y = area.y;
  const placed = list.map((mini, index) => {
    const spot = { id: mini.id, x: Math.round(area.x), y: Math.round(y), scale: Number(scales[index].toFixed(4)) };
    y += positive(mini.height, 1) * scales[index] + gap;
    return spot;
  });
  return { featured: main, minis: placed };
}

export const __testing = Object.freeze({ overlapArea, normalizeInput });
