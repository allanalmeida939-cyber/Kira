// Palco do briefing: onde cada cartão fica. Cartões montados todos com a mesma largura (a da coluna principal) e
// encaixados só com mover + escala uniforme, então nunca se sobrepõem, nunca passam da janela e nada rola.
export const STAGE_GAP = 12;
// Coluna das seções que já passaram (miniaturas à esquerda).
export const STAGE_SIDE_RATIO = 0.3;
const MAX_SCALE = 1;
const EPSILON = 1e-6;

/** Largura em que os cartões são montados: a da coluna principal, igual do começo ao fim (a animação só move/encolhe). */
export function stageCardWidth(width, gap = STAGE_GAP) {
  return Math.max(120, Math.round(width - Math.round(width * STAGE_SIDE_RATIO) - gap));
}

const fitScale = (cardWidth, areaWidth, areaHeight, contentHeight) => Math.max(0.05, Math.min(MAX_SCALE, areaWidth / cardWidth, areaHeight / Math.max(1, contentHeight)));

/** Todas as formas de dividir n cartões, na ordem, em `columns` colunas não vazias. */
function* splits(n, columns) {
  if (columns === 1) { yield [n]; return; }
  for (let first = 1; first <= n - columns + 1; first += 1) {
    for (const rest of splits(n - first, columns - 1)) yield [first, ...rest];
  }
}

function summaryLayout({ width, height, cardWidth, items, gap }) {
  let best = null;
  for (let columns = 1; columns <= Math.min(3, items.length); columns += 1) {
    const columnWidth = (width - gap * (columns - 1)) / columns;
    for (const sizes of splits(items.length, columns)) {
      let start = 0;
      const groups = sizes.map((size) => { const group = items.slice(start, start + size); start += size; return group; });
      const scales = groups.map((group) => fitScale(cardWidth, columnWidth, height - gap * (group.length - 1), group.reduce((sum, item) => sum + item.height, 0)));
      const worst = Math.min(...scales);
      const area = groups.reduce((sum, group, index) => sum + group.reduce((total, item) => total + cardWidth * item.height, 0) * scales[index] ** 2, 0);
      if (!best || worst > best.worst + EPSILON || (Math.abs(worst - best.worst) <= EPSILON && area > best.area)) best = { worst, area, groups, scales };
    }
  }
  const spots = new Map();
  const usedWidth = best.groups.reduce((sum, _group, index) => sum + cardWidth * best.scales[index], 0) + gap * (best.groups.length - 1);
  let x = Math.max(0, (width - usedWidth) / 2);
  best.groups.forEach((group, index) => {
    const scale = best.scales[index];
    let y = 0;
    group.forEach((item) => {
      spots.set(item.id, { x, y, scale });
      y += item.height * scale + gap;
    });
    x += cardWidth * scale + gap;
  });
  return spots;
}

/**
 * Onde cada cartão fica (canto superior esquerdo + escala uniforme ≤ 1).
 * - palco: o cartão da vez na coluna principal; os que já passaram encolhem para a coluna da esquerda, empilhados;
 * - resumo: todos juntos, nas colunas que deixam os cartões maiores.
 * @param {{width:number, height:number, cardWidth:number, items:{id:string,height:number}[], active?:string, summary?:boolean, gap?:number}} input
 * @returns {Map<string, {x:number, y:number, scale:number}>}
 */
export function stageLayout({ width, height, cardWidth, items, active = '', summary = false, gap = STAGE_GAP }) {
  const list = (Array.isArray(items) ? items : []).filter((item) => item && item.id);
  if (!list.length || !(width > 0) || !(height > 0) || !(cardWidth > 0)) return new Map();
  if (summary) return summaryLayout({ width, height, cardWidth, items: list, gap });
  const main = list.find((item) => item.id === active) || list[list.length - 1];
  const side = list.filter((item) => item !== main);
  const sideWidth = side.length ? Math.round(width * STAGE_SIDE_RATIO) : 0;
  const offset = side.length ? sideWidth + gap : 0;
  const mainWidth = width - offset;
  const mainScale = fitScale(cardWidth, mainWidth, height, main.height);
  const spots = new Map([[main.id, { x: offset + (mainWidth - cardWidth * mainScale) / 2, y: 0, scale: mainScale }]]);
  if (side.length) {
    const sideScale = fitScale(cardWidth, sideWidth, height - gap * (side.length - 1), side.reduce((sum, item) => sum + item.height, 0));
    let y = 0;
    side.forEach((item) => {
      spots.set(item.id, { x: (sideWidth - cardWidth * sideScale) / 2, y, scale: sideScale });
      y += item.height * sideScale + gap;
    });
  }
  return spots;
}
