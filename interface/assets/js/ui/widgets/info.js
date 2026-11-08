import { el, clear, setText, unavailable, WEATHER_TEXT, timeParts, svg, formatPercent, applyAppearance } from './kit.js';

const QUOTES = [
  'A persistência realiza o impossível.', 'Feito é melhor que perfeito.', 'Quem não arrisca não petisca.', 'O hábito faz o monge.',
  'Devagar se vai ao longe.', 'A prática leva à perfeição.', 'Conhecimento é poder.', 'Cada dia é uma nova chance.',
  'Foco no processo, não só no resultado.', 'Pequenos passos, grandes caminhos.', 'Quem planta, colhe.', 'O começo é metade do trabalho.',
  'Errar é humano; aprender é escolha.', 'A simplicidade é a sofisticação máxima.', 'Disciplina vence motivação.', 'Tudo parece impossível até ser feito.',
  'Fazer hoje o que o amanhã agradece.', 'Calma também é velocidade.', 'Quem pergunta aprende.', 'O melhor momento para começar é agora.',
];
const PHASES = ['Lua nova', 'Crescente', 'Quarto crescente', 'Gibosa crescente', 'Lua cheia', 'Gibosa minguante', 'Quarto minguante', 'Minguante'];
const NAMES = { 'USD-BRL': 'Dólar', 'EUR-BRL': 'Euro', 'BTC-BRL': 'Bitcoin' };
const HISTORY_POINTS = 30;

/** GET no backend local; qualquer falha vira "indisponível" só dentro do widget. */
async function load(path) {
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function sparkline(values) {
  const root = svg('svg', { viewBox: `0 0 ${HISTORY_POINTS} 12`, preserveAspectRatio: 'none', class: 'w-spark w-spark-mini' });
  const line = svg('polyline', {}, root);
  const min = Math.min(...values);
  const span = Math.max(...values) - min || 1;
  line.setAttribute('points', values.map((v, i) => `${HISTORY_POINTS - values.length + i},${(11 - ((v - min) / span) * 10).toFixed(1)}`).join(' '));
  return root;
}

function listWidget({ kind, title, path, size, render, every, optionDefs = [], styles, bare = false }) {
  return {
    kind, title, category: 'Informação', size, optionDefs, styles, bare,
    mount(box, ctx) {
      applyAppearance(box, ctx.options);
      box.classList.add(`w-${kind}`, `w-${kind}-${ctx.style || 'default'}`);
      const body = el('div', 'w-list');
      box.appendChild(body);
      let busy = false;
      const refresh = async () => {
        if (busy) return;
        busy = true;
        try { render(body, await load(typeof path === 'function' ? path(ctx) : path), ctx); } catch (_) { if (!body.firstChild) unavailable(body); } finally { busy = false; }
      };
      refresh();
      ctx.every(every, refresh);
    },
  };
}

const dayName = (iso) => new Date(`${iso}T12:00`).toLocaleDateString('pt-BR', { weekday: 'short', day: '2-digit' });

const weather = listWidget({
  kind: 'weather', title: 'Clima', size: { w: 320, h: 280 }, bare: true, every: 600000,
  path: (ctx) => `/api/ui/widgets/weather?city=${encodeURIComponent(ctx.options.city || 'São Paulo')}`,
  styles: [['compact', 'Compacto'], ['detailed', 'Detalhado'], ['forecast', 'Previsão 5 dias']],
  optionDefs: [{ key: 'city', label: 'Cidade', type: 'text', default: 'São Paulo' }],
  render(body, data, ctx) {
    clear(body);
    const current = data.current || {};
    const style = ctx.style || 'compact';
    const temp = `${Math.round(current.temperature_2m)}°C`;
    const text = WEATHER_TEXT[current.weather_code] || '';
    if (style === 'compact') {
      const row = el('div', 'w-weather-line');
      row.append(el('span', 'w-time w-small', temp), el('span', '', `${data.city} · ${text}`));
      body.appendChild(row);
      return;
    }
    body.append(el('div', 'w-muted w-center', data.city), el('div', 'w-time', temp), el('div', 'w-center', text));
    if (style === 'detailed') {
      body.append(el('div', 'w-muted w-center', `Sensação ${Math.round(current.apparent_temperature)}° · Umidade ${current.relative_humidity_2m}% · Vento ${Math.round(current.wind_speed_10m)} km/h`));
      const rise = String(data.daily?.sunrise?.[0] || '').slice(11, 16);
      const set = String(data.daily?.sunset?.[0] || '').slice(11, 16);
      if (rise) body.append(el('div', 'w-muted w-center', `Nascer ${rise} · Pôr ${set}`));
    }
    const days = (data.daily?.time || []).slice(1, style === 'forecast' ? 6 : 4);
    days.forEach((day, i) => {
      const row = el('div', 'w-row');
      row.append(el('span', '', dayName(day)), el('span', 'w-muted', WEATHER_TEXT[data.daily.weather_code?.[i + 1]] || ''), el('span', 'w-num', `${Math.round(data.daily.temperature_2m_min[i + 1])}° / ${Math.round(data.daily.temperature_2m_max[i + 1])}°`));
      body.appendChild(row);
    });
  },
});

const quoteHistory = {};
const quotes = listWidget({
  kind: 'quotes', title: 'Cotações', size: { w: 320, h: 220 }, bare: false, every: 60000, path: '/api/ui/widgets/quotes',
  styles: [['list', 'Lista com gráfico'], ['compact', 'Compacto']],
  render(body, data, ctx) {
    clear(body);
    (data.items || []).forEach((item) => {
      const bid = Number(item.bid);
      const history = (quoteHistory[item.pair] = [...(quoteHistory[item.pair] || []), bid].slice(-HISTORY_POINTS));
      const change = Number(item.pct);
      const row = el('div', 'w-row w-quote-row');
      const tone = change < 0 ? 'is-down' : 'is-up';
      row.append(el('span', '', `${NAMES[item.pair] || item.pair}`), el('span', `w-num ${tone}`, `R$ ${bid.toLocaleString('pt-BR', { maximumFractionDigits: 2 })}  ${change < 0 ? '▼' : '▲'} ${formatPercent(change)}`));
      body.appendChild(row);
      if ((ctx.style || 'list') === 'list' && history.length > 1) body.appendChild(sparkline(history));
    });
    if (!body.firstChild) unavailable(body);
  },
});

const scores = listWidget({
  kind: 'scores', title: 'Jogos de hoje', size: { w: 380, h: 300 }, every: 60000, path: '/api/ui/widgets/scores',
  render(body, data) {
    clear(body);
    if (!data.items?.length) { unavailable(body, 'sem jogos hoje'); return; }
    data.items.forEach((match) => {
      const row = el('div', 'w-match');
      const logo = (url) => {
        const img = el('img', 'w-logo');
        img.alt = '';
        img.referrerPolicy = 'no-referrer';
        if (typeof url === 'string' && url.startsWith('/api/ui/img?u=')) img.src = url; else img.hidden = true;
        return img;
      };
      row.append(logo(match.homeLogoUrl), el('span', 'w-team', String(match.home ?? '')), el('b', 'w-num', `${match.homeScore ?? '-'} : ${match.awayScore ?? '-'}`), el('span', 'w-team', String(match.away ?? '')), logo(match.awayLogoUrl));
      body.appendChild(row);
    });
  },
});

const history = listWidget({
  kind: 'history', title: 'Hoje na história', size: { w: 420, h: 320 }, every: 3600000, path: '/api/ui/widgets/history',
  render(body, data) {
    clear(body);
    (data.items || []).forEach((item) => {
      const row = el('div', 'w-hist');
      row.append(el('b', '', String(item.year)), document.createTextNode(` — ${item.text}`));
      body.appendChild(row);
    });
    if (!body.firstChild) unavailable(body);
  },
});

function moonInfo(date = new Date()) {
  const synodic = 29.530588853;
  const days = ((date.getTime() / 86400000 + 2440587.5 - 2451550.1) % synodic + synodic) % synodic;
  return { name: PHASES[Math.round((days / synodic) * 8) % 8], illumination: Math.round(((1 - Math.cos((2 * Math.PI * days) / synodic)) / 2) * 100) };
}

const moon = {
  kind: 'moon', title: 'Lua e sol', category: 'Informação', size: { w: 280, h: 190 }, bare: true,
  optionDefs: [{ key: 'city', label: 'Cidade (nascer/pôr do sol)', type: 'text', default: 'São Paulo' }],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const info = moonInfo();
    box.classList.add('w-moon');
    box.append(el('div', 'w-muted w-center', 'Fase da lua'), el('div', 'w-moon-name', info.name), el('div', 'w-center', `${info.illumination}% iluminada`));
    const sun = el('div', 'w-muted w-center', 'nascer/pôr do sol: …');
    box.appendChild(sun);
    load(`/api/ui/widgets/weather?city=${encodeURIComponent(ctx.options.city || 'São Paulo')}`).then((data) => {
      const rise = String(data.daily?.sunrise?.[0] || '').slice(11, 16);
      const set = String(data.daily?.sunset?.[0] || '').slice(11, 16);
      setText(sun, rise ? `Nascer ${rise} · Pôr ${set}` : 'sol indisponível');
    }).catch(() => setText(sun, 'sol indisponível'));
  },
};

const dayQuote = {
  kind: 'dayquote', title: 'Frase do dia', category: 'Informação', size: { w: 340, h: 160 }, bare: true,
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const p = timeParts();
    box.appendChild(el('div', 'w-quote', `“${QUOTES[(Number(p.day) + Number(p.year) * 7 + p.month.length) % QUOTES.length]}”`));
  },
};

export const INFO_WIDGETS = [weather, quotes, scores, moon, dayQuote, history];
export { moonInfo };
