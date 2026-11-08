import { el, clear, unavailable, applyAppearance } from './kit.js';

const CAROUSEL_MS = 5000;
const SECTIONS = [['specials', 'Ofertas'], ['coming_soon', 'Em breve'], ['top_sellers', 'Mais vendidos'], ['new_releases', 'Novidades']];
const money = (value) => Number(value).toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' });

async function getJson(path) {
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function cover(item, className) {
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

function link(button, item, ctx) {
  button.type = 'button';
  button.title = item.title;
  button.addEventListener('click', () => ctx.openExternal(item.link));
  return button;
}

function priceTag(item, section) {
  if (section === 'coming_soon') return el('span', 'w-news-tag', 'EM BREVE');
  const box = el('span', 'w-game-price');
  if (item.discount) box.appendChild(el('span', 'w-news-tag is-feature', `-${item.discount}%`));
  if (item.original && item.discount) box.appendChild(el('s', 'w-muted', money(item.original)));
  box.appendChild(el('b', '', item.price ? money(item.price) : 'Grátis'));
  return box;
}

function gameTile(item, ctx, section) {
  const button = link(el('button', 'w-news-tile'), item, ctx);
  const frame = el('span', 'w-news-frame');
  frame.appendChild(cover(item, 'w-news-img'));
  const text = el('span', 'w-news-text');
  text.append(el('b', 'w-news-title', item.title), priceTag(item, section));
  button.append(frame, text);
  return button;
}

function gameSlide(item, ctx, section) {
  const button = link(el('button', 'w-news-slide'), item, ctx);
  const caption = el('span', 'w-news-hero-caption');
  caption.append(el('b', '', item.title), priceTag(item, section));
  button.append(cover(item, 'w-news-hero-img'), caption);
  return button;
}

function gameRow(item, ctx, section) {
  const button = link(el('button', 'w-news-card'), item, ctx);
  const text = el('span', 'w-news-text');
  text.append(el('b', 'w-news-title', item.title), priceTag(item, section));
  button.append(cover(item, 'w-news-thumb'), text);
  return button;
}

const GAME_RENDERERS = {
  tiles(body, items, ctx, section) { items.forEach((item) => body.appendChild(gameTile(item, ctx, section))); },
  list(body, items, ctx, section) { items.forEach((item) => body.appendChild(gameRow(item, ctx, section))); },
  carousel(body, items, ctx, section, state) {
    const track = el('div', 'w-news-track');
    items.slice(0, 8).forEach((item) => track.appendChild(gameSlide(item, ctx, section)));
    body.appendChild(track);
    let index = 0;
    state.tick = () => { index = (index + 1) % Math.max(track.childElementCount, 1); track.style.transform = `translateX(${-index * 100}%)`; };
  },
};

const games = {
  kind: 'games', title: 'Jogos e ofertas', category: 'Entretenimento', size: { w: 460, h: 440 }, bare: false,
  styles: [['tiles', 'Cartões de jogos'], ['carousel', 'Carrossel de pôsteres'], ['list', 'Lista']],
  optionDefs: [{ key: 'section', label: 'Aba inicial (specials, coming_soon, top_sellers, new_releases)', type: 'text', default: 'specials' }],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const style = GAME_RENDERERS[ctx.style] ? ctx.style : 'tiles';
    box.classList.add('w-news', 'w-games', `w-games-${style}`);
    const bar = el('div', 'w-news-bar');
    const tabs = el('div', 'w-news-filters');
    bar.appendChild(tabs);
    const body = el('div', 'w-news-body');
    box.append(bar, body);
    const state = { tick: null, section: SECTIONS.some(([key]) => key === ctx.options.section) ? ctx.options.section : 'specials' };
    ctx.every(CAROUSEL_MS, () => state.tick?.());
    let busy = false;
    const draw = (data) => {
      state.tick = null;
      clear(body);
      body.scrollTop = 0;
      GAME_RENDERERS[style](body, data.items || [], ctx, state.section, state);
      if (!body.firstChild) unavailable(body, 'nada por aqui agora');
    };
    const drawTabs = () => {
      clear(tabs);
      SECTIONS.forEach(([key, label]) => {
        const chip = el('button', `w-chip${state.section === key ? ' is-active' : ''}`, label);
        chip.type = 'button';
        chip.addEventListener('click', () => { state.section = key; drawTabs(); refresh(); });
        tabs.appendChild(chip);
      });
    };
    const refresh = async () => {
      if (busy) return;
      busy = true;
      try { draw(await getJson(`/api/ui/widgets/games?section=${state.section}`)); } catch (_) { if (!body.firstChild) unavailable(body); } finally { busy = false; }
    };
    drawTabs();
    refresh();
    ctx.every(900000, refresh);
  },
};

function when(epoch) {
  const date = new Date(epoch * 1000);
  return date.toLocaleDateString('pt-BR', { day: '2-digit', month: 'short' });
}

function leftText(item) {
  if (!item.now) return `Começa em ${when(item.start)}`;
  const days = Math.max(0, Math.ceil((item.end * 1000 - Date.now()) / 86400000));
  return days <= 1 ? 'Termina hoje' : `Termina em ${days} dias`;
}

const freegames = {
  kind: 'freegames', title: 'Jogos grátis', category: 'Entretenimento', size: { w: 460, h: 340 }, bare: false,
  styles: [['cards', 'Cartões'], ['list', 'Lista']],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const style = ctx.style === 'list' ? 'list' : 'cards';
    box.classList.add('w-news', 'w-freegames', `w-freegames-${style}`);
    const body = el('div', 'w-news-body');
    box.appendChild(body);
    let busy = false;
    const refresh = async () => {
      if (busy) return;
      busy = true;
      try {
        const data = await getJson('/api/ui/widgets/free-games');
        clear(body);
        (data.items || []).forEach((item) => {
          const button = link(el('button', style === 'list' ? 'w-news-card' : 'w-news-tile'), item, ctx);
          const text = el('span', 'w-news-text');
          text.append(el('b', 'w-news-title', item.title), el('span', item.now ? 'w-game-left' : 'w-muted', leftText(item)));
          if (style === 'list') button.append(cover(item, 'w-news-thumb'), text);
          else {
            const frame = el('span', 'w-news-frame');
            const badges = el('span', 'w-news-badges');
            badges.appendChild(el('span', `w-news-tag${item.now ? ' is-feature' : ''}`, item.now ? 'GRÁTIS AGORA' : 'EM BREVE'));
            frame.append(cover(item, 'w-news-img'), badges);
            button.append(frame, text);
          }
          body.appendChild(button);
        });
        if (!body.firstChild) unavailable(body, 'sem jogos grátis agora');
      } catch (_) { if (!body.firstChild) unavailable(body); } finally { busy = false; }
    };
    refresh();
    ctx.every(1800000, refresh);
  },
};

const compact = (value) => Number(value).toLocaleString('pt-BR', { notation: 'compact', maximumFractionDigits: 1 });

const trending = {
  kind: 'trending', title: 'Em alta', category: 'Informação', size: { w: 340, h: 400 }, bare: false,
  styles: [['ranking', 'Ranking com barras'], ['chips', 'Nuvem de assuntos']],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const style = ctx.style === 'chips' ? 'chips' : 'ranking';
    box.classList.add('w-trending', `w-trending-${style}`);
    box.appendChild(el('div', 'w-muted w-trending-caption', 'Mais lidos ontem na Wikipédia'));
    const body = el('div', 'w-trending-body');
    box.appendChild(body);
    let busy = false;
    const refresh = async () => {
      if (busy) return;
      busy = true;
      try {
        const data = await getJson('/api/ui/widgets/trending');
        clear(body);
        const top = Math.max(...data.items.map((item) => item.views), 1);
        data.items.forEach((item, index) => {
          const button = link(el('button', 'w-trend'), item, ctx);
          if (style === 'chips') {
            button.classList.add('w-chip');
            button.textContent = item.title;
          } else {
            const bar = el('span', 'w-trend-bar');
            bar.style.transform = `scaleX(${(item.views / top).toFixed(3)})`;
            button.append(el('span', 'w-trend-rank', String(index + 1)), el('span', 'w-trend-title', item.title), el('span', 'w-muted', compact(item.views)), bar);
          }
          body.appendChild(button);
        });
      } catch (_) { if (!body.firstChild) unavailable(body); } finally { busy = false; }
    };
    refresh();
    ctx.every(1800000, refresh);
  },
};

export const GAMES_WIDGETS = [games, freegames, trending];
