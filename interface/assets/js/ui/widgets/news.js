import { el, clear, unavailable, relativeTime, applyAppearance } from './kit.js';

const STYLES = [
  ['gx', 'Cartões com destaque'], ['grid', 'Grade de cartões'], ['hero', 'Destaque grande + lista'],
  ['list', 'Lista com imagem'], ['carousel', 'Carrossel'], ['ticker', 'Ticker horizontal'],
];
const CAROUSEL_MS = 6000;
const FOR_YOU = '__foryou';

async function getJson(path, options) {
  const response = await fetch(path, { cache: 'no-store', ...options });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function picture(item, className) {
  const image = el('img', className);
  image.alt = '';
  image.loading = 'lazy';
  image.decoding = 'async';
  image.referrerPolicy = 'no-referrer';
  if (item.image) {
    image.src = item.image;
    image.addEventListener('error', () => { image.hidden = true; });
  } else {
    image.hidden = true;
  }
  return image;
}

const meta = (item) => `${item.source || ''}${item.ts ? ` · ${relativeTime(item.ts)}` : ''}`;

function badges(item, labels, featured) {
  const row = el('span', 'w-news-badges');
  if (featured) row.appendChild(el('span', 'w-news-tag is-feature', 'DESTAQUE'));
  else if (item.match) row.appendChild(el('span', 'w-news-tag is-you', 'PARA VOCÊ'));
  const label = labels[item.topic];
  if (label) row.appendChild(el('span', 'w-news-tag', label.toUpperCase()));
  return row;
}

function open(button, item, ctx) {
  button.type = 'button';
  button.title = item.title;
  button.addEventListener('click', () => ctx.openExternal(item.link));
  return button;
}

function tile(item, ctx, labels, className, featured = false) {
  const button = open(el('button', className), item, ctx);
  button.dataset.topic = item.topic || '';
  const text = el('span', 'w-news-text');
  text.append(el('b', 'w-news-title', item.title), el('span', 'w-muted', meta(item)));
  const frame = el('span', 'w-news-frame');
  frame.append(picture(item, 'w-news-img'), badges(item, labels, featured));
  button.append(frame, text);
  return button;
}

function overlay(item, ctx, labels, className, featured) {
  const button = open(el('button', className), item, ctx);
  button.dataset.topic = item.topic || '';
  const caption = el('span', 'w-news-hero-caption');
  caption.append(el('b', '', item.title), el('span', 'w-muted', meta(item)));
  button.append(picture(item, 'w-news-hero-img'), badges(item, labels, featured), caption);
  return button;
}

function card(item, ctx, labels, className) {
  const button = open(el('button', className), item, ctx);
  button.dataset.topic = item.topic || '';
  const text = el('span', 'w-news-text');
  text.append(el('b', 'w-news-title', item.title), el('span', 'w-muted', meta(item)));
  button.append(picture(item, 'w-news-thumb'), text);
  return button;
}

function renderGx(body, items, ctx, labels) {
  const [first, ...rest] = items;
  if (first) body.appendChild(overlay(first, ctx, labels, 'w-news-hero is-wide', true));
  rest.forEach((item) => body.appendChild(tile(item, ctx, labels, 'w-news-tile')));
}

function renderGrid(body, items, ctx, labels) {
  items.forEach((item) => body.appendChild(tile(item, ctx, labels, 'w-news-tile')));
}

function renderList(body, items, ctx, labels) {
  items.forEach((item) => body.appendChild(card(item, ctx, labels, 'w-news-card')));
}

function renderHero(body, items, ctx, labels) {
  const [first, ...rest] = items;
  if (first) body.appendChild(overlay(first, ctx, labels, 'w-news-hero', true));
  rest.slice(0, 10).forEach((item) => body.appendChild(card(item, ctx, labels, 'w-news-card')));
}

function renderCarousel(body, items, ctx, labels, state) {
  const track = el('div', 'w-news-track');
  items.slice(0, 8).forEach((item) => track.appendChild(overlay(item, ctx, labels, 'w-news-slide', false)));
  body.appendChild(track);
  let index = 0;
  const count = track.childElementCount;
  state.tick = () => { index = (index + 1) % Math.max(count, 1); track.style.transform = `translateX(${-index * 100}%)`; };
}

function renderTicker(body, items, ctx) {
  const track = el('div', 'w-news-ticker');
  [...items, ...items].forEach((item) => {
    const link = open(el('button', 'w-news-tick', item.title), item, ctx);
    track.appendChild(link);
  });
  track.style.animationDuration = `${Math.max(30, items.length * 6)}s`;
  body.appendChild(track);
}

const RENDERERS = { gx: renderGx, grid: renderGrid, hero: renderHero, list: renderList, carousel: renderCarousel, ticker: renderTicker };

function interestsPanel(ctx, refreshNews) {
  const panel = el('div', 'w-interests');
  panel.hidden = true;
  const chips = el('div', 'w-chips');
  const form = el('form', 'w-form');
  const input = el('input', 'w-input');
  input.placeholder = 'Novo interesse (ex.: futebol)';
  input.maxLength = 40;
  const add = el('button', 'w-button', '+');
  add.type = 'submit';
  const clearButton = el('button', 'w-button', 'Limpar');
  clearButton.type = 'button';
  form.append(input, add, clearButton);
  panel.append(el('div', 'w-muted', 'A Kira aprende o que você pesquisa e pede. Edite ou limpe quando quiser.'), chips, form);
  const draw = (topics) => {
    clear(chips);
    topics.forEach((topic) => {
      const chip = el('button', 'w-chip', `${topic.term} ×`);
      chip.type = 'button';
      chip.addEventListener('click', () => save(topics.filter((t) => t.term !== topic.term).map((t) => t.term)));
      chips.appendChild(chip);
    });
    if (!topics.length) chips.appendChild(el('span', 'w-muted', 'nenhum interesse ainda'));
  };
  const save = async (terms) => {
    try { draw((await getJson('/api/ui/interests', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ terms }) })).topics); refreshNews(); } catch (_) { /* mantém a lista */ }
  };
  const load = async () => { try { draw((await getJson('/api/ui/interests')).topics); } catch (_) { draw([]); } };
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) return;
    input.value = '';
    const current = (await getJson('/api/ui/interests').catch(() => ({ topics: [] }))).topics.map((t) => t.term);
    save([...current, value]);
  });
  clearButton.addEventListener('click', async () => { try { await getJson('/api/ui/interests', { method: 'DELETE' }); draw([]); refreshNews(); } catch (_) { /* ignora */ } });
  return { panel, toggle() { panel.hidden = !panel.hidden; if (!panel.hidden) load(); } };
}

const news = {
  kind: 'news', title: 'Últimas notícias', category: 'Informação', size: { w: 460, h: 460 }, bare: false, styles: STYLES,
  optionDefs: [
    { key: 'topics', label: 'Temas (geral, tecnologia, games, esportes, ciencia, economia); vazio = todos', type: 'text' },
    { key: 'feeds', label: 'Fontes RSS (https), separadas por vírgula; vazio = padrão', type: 'textarea' },
    { key: 'filters', label: 'Mostrar filtros de tema', type: 'bool', default: true },
  ],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const style = ctx.style && RENDERERS[ctx.style] ? ctx.style : 'gx';
    box.classList.add('w-news', `w-news-${style}`);
    const bar = el('div', 'w-news-bar');
    const filters = el('div', 'w-news-filters');
    const toggle = el('button', 'w-link w-news-interests-button', 'Meus interesses');
    toggle.type = 'button';
    bar.append(filters, toggle);
    const body = el('div', 'w-news-body');
    box.append(bar, body);
    const state = { tick: null, data: null, active: '' };
    ctx.every(CAROUSEL_MS, () => state.tick?.());
    const draw = () => {
      const data = state.data;
      if (!data) return;
      const labels = Object.fromEntries((data.topics || []).map((t) => [t.key, t.label]));
      const items = (data.items || []).filter((item) => !state.active || (state.active === FOR_YOU ? item.match : item.topic === state.active));
      state.tick = null;
      clear(body);
      body.scrollTop = 0;
      RENDERERS[style](body, items, ctx, labels, state);
      if (!body.firstChild) unavailable(body, 'nada neste tema agora');
    };
    const drawFilters = () => {
      clear(filters);
      const data = state.data;
      if (ctx.options.filters === false || !data) return;
      const options = [['', 'Todos'], ...(data.items.some((i) => i.match) ? [[FOR_YOU, 'Para você']] : []), ...(data.topics || []).map((t) => [t.key, t.label])];
      if (options.length < 3) return;
      options.forEach(([key, label]) => {
        const chip = el('button', `w-chip${state.active === key ? ' is-active' : ''}`, label);
        chip.type = 'button';
        chip.addEventListener('click', () => { state.active = key; drawFilters(); draw(); });
        filters.appendChild(chip);
      });
    };
    let busy = false;
    const refresh = async () => {
      if (busy) return;
      busy = true;
      try {
        const feeds = String(ctx.options.feeds || '').split(/[\s,]+/).filter(Boolean).join(',');
        const topics = String(ctx.options.topics || '').split(/[\s,]+/).filter(Boolean).join(',');
        state.data = await getJson(`/api/ui/widgets/news?feeds=${encodeURIComponent(feeds)}&topics=${encodeURIComponent(topics)}&limit=30`);
        if (state.active && state.active !== FOR_YOU && !state.data.topics.some((t) => t.key === state.active)) state.active = '';
        drawFilters();
        draw();
      } catch (_) {
        if (!body.firstChild) unavailable(body);
      } finally { busy = false; }
    };
    const interests = interestsPanel(ctx, refresh);
    box.insertBefore(interests.panel, body);
    toggle.addEventListener('click', () => interests.toggle());
    refresh();
    ctx.every(300000, refresh);
  },
};

export const NEWS_WIDGETS = [news];
