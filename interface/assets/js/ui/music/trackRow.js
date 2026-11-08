// Linha de faixa da aba Música (resultados da busca e tocadas recentes): clicar na linha toca; "+" guarda numa playlist.
// Título, canal e capa vêm do YouTube: tudo por textContent e capa só https (antes ia por innerHTML sem escapar).

const SAFE_THUMB = /^https:\/\/[^\s"'<>]+$/;
const SVG_NS = 'http://www.w3.org/2000/svg';

function el(doc, tag, className = '', text = '') {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function plusIcon(doc) {
  const svg = doc.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const path = doc.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', 'M12 5v14 M5 12h14');
  svg.appendChild(path);
  return svg;
}

const NAMED_ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };

/** "MC&#39;s" -> "MC's" sem passar por HTML (faixas antigas guardadas ainda vêm assim da busca do YouTube). */
export function decodeEntities(value) {
  return String(value ?? '').replace(/&(#x[0-9a-f]{1,6}|#\d{1,7}|amp|lt|gt|quot|apos);/gi, (match, code) => {
    const lower = code.toLowerCase();
    if (lower in NAMED_ENTITIES) return NAMED_ENTITIES[lower];
    const point = lower.startsWith('#x') ? Number.parseInt(lower.slice(2), 16) : Number.parseInt(lower.slice(1), 10);
    return Number.isInteger(point) && point > 0 && point <= 0x10ffff ? String.fromCodePoint(point) : match;
  });
}

/** Dados da linha já limpos: texto curto, sem espaços sobrando, e capa só de endereço https. */
export function trackRowModel(item = {}) {
  const text = (value, limit) => decodeEntities(value).replace(/\s+/g, ' ').trim().slice(0, limit);
  const thumb = String(item?.thumbnail || item?.thumb || '');
  return {
    title: text(item?.title, 160) || 'Sem título',
    sub: text(item?.channel || item?.artist, 80) || 'Canal desconhecido',
    duration: text(item?.duration, 12),
    thumb: SAFE_THUMB.test(thumb) ? thumb : '',
  };
}

/**
 * Linha clicável: Enter/Espaço ou clique tocam; o botão "+" abre a escolha de playlist (sem tocar).
 * A faixa que está tocando ganha o equalizador animado (só transform, pausa junto com a música).
 */
export function createTrackRow(doc, item, { index = 0, active = false, onPlay = null, onAdd = null } = {}) {
  const model = trackRowModel(item);
  const row = el(doc, 'div', `mus-track${active ? ' is-active' : ''}`);
  row.setAttribute('role', 'button');
  row.setAttribute('aria-label', `Tocar ${model.title}`);
  row.tabIndex = 0;
  const art = el(doc, 'span', 'mus-track-art');
  if (model.thumb) {
    const img = el(doc, 'img');
    img.src = model.thumb;
    img.alt = '';
    img.loading = 'lazy';
    art.appendChild(img);
  }
  const eq = el(doc, 'span', 'mus-eq');
  eq.setAttribute('aria-hidden', 'true');
  eq.append(el(doc, 'i'), el(doc, 'i'), el(doc, 'i'));
  art.append(el(doc, 'span', 'mus-track-play'), eq);
  const info = el(doc, 'span', 'mus-track-info');
  info.append(el(doc, 'span', 'mus-track-title', model.title), el(doc, 'span', 'mus-track-sub', model.sub));
  const add = el(doc, 'button', 'mus-track-add');
  add.type = 'button';
  add.title = 'Adicionar a uma playlist';
  add.setAttribute('aria-label', `Adicionar ${model.title} a uma playlist`);
  add.appendChild(plusIcon(doc));
  row.append(el(doc, 'span', 'mus-track-num', String(index + 1)), art, info, el(doc, 'span', 'mus-track-time', model.duration), add);
  const play = () => onPlay?.();
  row.addEventListener('click', (event) => {
    if (!event.target?.closest?.('.mus-track-add')) play();
  });
  row.addEventListener('keydown', (event) => {
    if (event.target !== row || (event.key !== 'Enter' && event.key !== ' ')) return;
    event.preventDefault?.();
    play();
  });
  add.addEventListener('click', (event) => {
    event.stopPropagation?.();
    onAdd?.(event.currentTarget || add);
  });
  return row;
}

if (globalThis.window && !globalThis.window.KiraTrackRow) globalThis.window.KiraTrackRow = { createTrackRow };
