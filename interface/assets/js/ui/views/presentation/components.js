import { clear, create, safeHttps, safeImage, safeText } from '../panelDom.js';
import { createVideoPlayer } from '../videoPlayer.js';
import { renderTitleIcon } from '../../windowing/titleIcons.js';

/** Abre o link no navegador do sistema pelo backend local; sem backend, usa a janela atual. */
export function openExternal(url, { fetchRef = globalThis.fetch, windowRef = globalThis.window } = {}) {
  const safe = safeHttps(url);
  if (!safe) return false;
  const fallback = () => windowRef?.open?.(safe, '_blank', 'noopener,noreferrer');
  if (typeof fetchRef !== 'function') { fallback(); return true; }
  fetchRef('/api/ui/open-external', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: safe }) })
    .then((response) => { if (!response.ok) fallback(); })
    .catch(fallback);
  return true;
}

function formatDate(value) {
  const date = new Date(String(value || ''));
  return Number.isNaN(date.getTime()) ? safeText(value) : date.toLocaleDateString('pt-BR', { day: '2-digit', month: 'short', year: 'numeric' });
}

/** Cada componente: mount(el, data, ctx) devolve { update(data), unmount() }. Só textContent/createElement. */
const textCard = {
  mount(el, data, { documentRef }) {
    const body = create(documentRef, 'div', 'kui-pres-text');
    el.appendChild(body);
    const update = (next = {}) => {
      clear(body);
      const paragraphs = Array.isArray(next.paragraphs) ? next.paragraphs : String(next.body || '').split(/\n{2,}/);
      paragraphs.map((p) => safeText(p)).filter(Boolean).forEach((p) => body.appendChild(create(documentRef, 'p', '', p)));
      const points = Array.isArray(next.points) ? next.points.map((p) => safeText(p)).filter(Boolean) : [];
      if (points.length) {
        const list = create(documentRef, 'ul', 'kui-pres-points');
        points.forEach((p) => list.appendChild(create(documentRef, 'li', '', p)));
        body.appendChild(list);
      }
    };
    update(data);
    return { update, unmount() { clear(body); } };
  },
};

const sourceList = {
  mount(el, data, { documentRef, open }) {
    const list = create(documentRef, 'ol', 'kui-pres-sources');
    el.appendChild(list);
    const update = (next = {}) => {
      clear(list);
      (Array.isArray(next.items) ? next.items : []).forEach((source) => {
        const url = safeHttps(source?.url);
        const item = create(documentRef, 'li');
        const button = create(documentRef, 'button', 'kui-pres-source', safeText(source?.name, 'Fonte'));
        button.type = 'button';
        button.disabled = !url;
        button.title = url ? `Abrir ${url} no navegador` : 'link indisponível';
        button.addEventListener?.('click', () => open(url));
        item.appendChild(button);
        const dates = [source?.publishedAt && `publicado ${formatDate(source.publishedAt)}`, source?.accessedAt && `consultado ${formatDate(source.accessedAt)}`].filter(Boolean).join(' · ');
        if (dates) item.appendChild(create(documentRef, 'span', 'kui-pres-muted', dates));
        list.appendChild(item);
      });
      if (!list.firstChild) list.appendChild(create(documentRef, 'li', 'kui-pres-muted', 'Nenhuma fonte ainda.'));
    };
    update(data);
    return { update, unmount() { clear(list); } };
  },
};

const timeline = {
  mount(el, data, { documentRef }) {
    const list = create(documentRef, 'ol', 'kui-pres-timeline');
    el.appendChild(list);
    const update = (next = {}) => {
      clear(list);
      const events = (Array.isArray(next.events) ? next.events : [])
        .map((event) => ({ date: safeText(event?.date), label: safeText(event?.label || event?.text), order: Number(event?.order ?? event?.year) }))
        .filter((event) => event.label)
        .sort((a, b) => (Number.isFinite(a.order) && Number.isFinite(b.order) ? a.order - b.order : 0));
      events.forEach((event) => {
        const item = create(documentRef, 'li');
        item.appendChild(create(documentRef, 'b', 'kui-pres-when', event.date));
        item.appendChild(create(documentRef, 'span', '', event.label));
        list.appendChild(item);
      });
    };
    update(data);
    return { update, unmount() { clear(list); } };
  },
};

const imageGallery = {
  mount(el, data, { documentRef }) {
    const grid = create(documentRef, 'div', 'kui-pres-gallery');
    el.appendChild(grid);
    const update = (next = {}) => {
      clear(grid);
      (Array.isArray(next.items) ? next.items : []).forEach((item) => {
        const src = safeImage(item?.imageUrl);
        if (!src) return;
        const figure = create(documentRef, 'figure');
        const image = create(documentRef, 'img');
        image.src = src;
        image.alt = safeText(item?.caption);
        image.loading = 'lazy';
        image.decoding = 'async';
        figure.appendChild(image);
        if (item?.caption) figure.appendChild(create(documentRef, 'figcaption', '', safeText(item.caption)));
        grid.appendChild(figure);
      });
    };
    update(data);
    return { update, unmount() { clear(grid); } };
  },
};

const WEATHER_ICONS = [[[0, 1], '☀'], [[2], '⛅'], [[3, 45, 48], '☁'], [[51, 53, 55, 61, 63, 65, 66, 67, 80, 81, 82], '🌧'], [[71, 73, 75], '❄'], [[95, 96, 99], '⛈']];
const weatherIcon = (code) => (WEATHER_ICONS.find(([codes]) => codes.includes(code)) || [null, '·'])[1];
const degrees = (value) => (typeof value === 'number' ? `${Math.round(value)}°` : '—');
const percent = (value) => (typeof value === 'number' ? `${Math.round(value)}%` : '—');
const dayLabel = (iso, index) => (index === 0 ? 'Hoje' : index === 1 ? 'Amanhã' : new Date(`${iso}T12:00`).toLocaleDateString('pt-BR', { weekday: 'short', day: '2-digit' }));

const weatherCard = {
  mount(el, data, { documentRef: doc }) {
    const root = create(doc, 'div', 'kui-pres-weather');
    el.appendChild(root);
    const update = (next = {}) => {
      clear(root);
      const days = Array.isArray(next.forecast) ? next.forecast : [];
      const focus = Math.min(Math.max(Number(next.focusDay) || 0, 0), Math.max(days.length - 1, 0));
      const day = days[focus] || {};
      const current = next.current || {};
      const place = [next.location?.name, next.location?.region].map((p) => safeText(p)).filter(Boolean).join(', ');
      const hero = create(doc, 'div', 'kui-wx-hero');
      hero.appendChild(create(doc, 'span', 'kui-wx-icon', weatherIcon(focus === 0 ? current.code : day.code)));
      const main = create(doc, 'div');
      main.appendChild(create(doc, 'b', 'kui-wx-place', place));
      main.appendChild(create(doc, 'span', 'kui-wx-temp', focus === 0 ? degrees(current.temperature) : `${degrees(day.min)} / ${degrees(day.max)}`));
      main.appendChild(create(doc, 'span', 'kui-pres-muted', `${dayLabel(day.date, focus)} · ${safeText(focus === 0 ? current.condition : day.condition, 'condição indisponível')}`));
      hero.appendChild(main);
      const facts = create(doc, 'dl', 'kui-wx-facts');
      [['Mín / máx', `${degrees(day.min)} / ${degrees(day.max)}`], ['Chance de chuva', percent(day.precipitationProbability)], ...(focus === 0 ? [['Sensação', degrees(current.apparentTemperature)], ['Umidade', percent(current.humidity)]] : [])]
        .forEach(([label, value]) => { facts.appendChild(create(doc, 'dt', '', label)); facts.appendChild(create(doc, 'dd', '', value)); });
      hero.appendChild(facts);
      root.appendChild(hero);
      const periods = create(doc, 'div', 'kui-wx-periods');
      (day.periods || []).forEach((period) => {
        const cell = create(doc, 'div', 'kui-wx-period');
        cell.appendChild(create(doc, 'span', 'kui-pres-muted', safeText(period.period)));
        cell.appendChild(create(doc, 'span', 'kui-wx-icon-sm', weatherIcon(period.code)));
        cell.appendChild(create(doc, 'b', '', degrees(period.temperature)));
        cell.appendChild(create(doc, 'span', 'kui-pres-muted', `chuva ${percent(period.precipitationProbability)}`));
        periods.appendChild(cell);
      });
      if (periods.firstChild) root.appendChild(periods);
      const week = create(doc, 'div', 'kui-wx-days');
      days.forEach((item, index) => {
        const cell = create(doc, 'div', `kui-wx-day${index === focus ? ' is-focus' : ''}`);
        cell.appendChild(create(doc, 'span', 'kui-pres-muted', dayLabel(item.date, index)));
        cell.appendChild(create(doc, 'span', 'kui-wx-icon-sm', weatherIcon(item.code)));
        cell.appendChild(create(doc, 'b', '', `${degrees(item.min)} / ${degrees(item.max)}`));
        cell.appendChild(create(doc, 'span', 'kui-pres-muted', percent(item.precipitationProbability)));
        week.appendChild(cell);
      });
      root.appendChild(week);
      const updated = next.updatedAt ? new Date(next.updatedAt).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' }) : '';
      root.appendChild(create(doc, 'p', 'kui-pres-muted', `Atualizado às ${updated || '—'} · Fonte: ${safeText(next.source?.name, 'indisponível')}`));
    };
    update(data);
    return { update, unmount() { clear(root); } };
  },
};

const foldWords = (text) => String(text || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 2);

// Miniatura de 480 px gerada pelo proxy local (cartão mostra 112 px; o original chega a 500 KB).
const newsThumb = (src) => (src.startsWith('/api/ui/img?u=') ? `${src}&w=480` : src);

/** Sem foto da matéria: selo do veículo (cor própria + ícone de jornal + nome), nunca imagem inventada. */
function outletBadge(doc, publisher) {
  const name = safeText(publisher) || 'Notícia';
  let hue = 0;
  for (const char of name) hue = (hue * 31 + char.codePointAt(0)) % 360;
  const badge = create(doc, 'span', 'kui-news-img is-empty');
  badge.style?.setProperty?.('--kui-outlet-hue', String(hue));
  const icon = create(doc, 'span', 'kui-news-outlet-icon');
  renderTitleIcon(icon, 'newspaper', doc);
  badge.appendChild(icon);
  badge.appendChild(create(doc, 'span', 'kui-news-outlet', name.slice(0, 18)));
  return badge;
}

const newsCards = {
  mount(el, data, { documentRef: doc, open, windowRef = doc.defaultView || globalThis }) {
    const list = create(doc, 'div', 'kui-pres-news');
    el.appendChild(list);
    let cards = [];
    const update = (next = {}) => {
      clear(list);
      cards = (Array.isArray(next.articles) ? next.articles : []).map((article, index) => {
        const url = safeHttps(article?.canonicalUrl);
        const card = create(doc, 'button', 'kui-news-card');
        card.type = 'button';
        card.disabled = !url;
        card.title = url ? 'Abrir a matéria original no navegador' : 'link indisponível';
        card.addEventListener?.('click', () => open(url));
        const src = safeImage(article?.imageUrl);
        if (src) {
          // Os 3 primeiros carregam já; o esqueleto some no load e a falha vira o selo do veículo.
          const image = create(doc, 'img', 'kui-news-img is-loading');
          image.alt = ''; image.decoding = 'async'; image.loading = index < 3 ? 'eager' : 'lazy';
          image.addEventListener?.('load', () => image.classList?.remove?.('is-loading'));
          image.addEventListener?.('error', () => image.replaceWith?.(outletBadge(doc, article?.publisher)));
          image.src = newsThumb(src);
          card.appendChild(image);
        } else {
          card.appendChild(outletBadge(doc, article?.publisher));
        }
        const text = create(doc, 'span', 'kui-news-text');
        const tags = create(doc, 'span', 'kui-news-tags');
        if (article?.label) tags.appendChild(create(doc, 'span', 'kui-news-tag', `${article.label} (automático)`));
        if (article?.preliminary) tags.appendChild(create(doc, 'span', 'kui-news-tag is-warn', 'preliminar: fonte única'));
        const extra = Array.isArray(article?.sources) ? article.sources.length - 1 : 0;
        if (extra > 0) tags.appendChild(create(doc, 'span', 'kui-news-tag', `+${extra} fonte${extra > 1 ? 's' : ''}`));
        if (tags.firstChild) text.appendChild(tags);
        text.appendChild(create(doc, 'b', 'kui-news-title', safeText(article?.title)));
        text.appendChild(create(doc, 'span', 'kui-pres-muted', [safeText(article?.publisher), article?.publishedAt ? formatDate(article.publishedAt) : ''].filter(Boolean).join(' · ')));
        if (article?.summary) text.appendChild(create(doc, 'span', 'kui-news-summary', safeText(article.summary)));
        card.appendChild(text);
        list.appendChild(card);
        return { card, keys: new Set((Array.isArray(article?.keywords) ? article.keywords : foldWords(article?.title))) };
      });
    };
    const onSpoken = (event) => {
      const heard = new Set(foldWords(event?.detail?.text).slice(-30));
      let best = null; let bestHits = 1;
      cards.forEach((entry) => {
        const hits = [...entry.keys].filter((key) => heard.has(key)).length;
        if (hits > bestHits) { best = entry; bestHits = hits; }
      });
      if (!best) return;
      cards.forEach((entry) => entry.card.classList?.toggle?.('is-cited', entry === best));
      best.card.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
    };
    windowRef.addEventListener?.('kira:spoken', onSpoken);
    update(data);
    return { update, unmount() { windowRef.removeEventListener?.('kira:spoken', onSpoken); clear(list); } };
  },
};

const timeOf = (iso) => { const date = new Date(String(iso || '')); return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' }); };
const duration = (start, end) => { const minutes = Math.round((new Date(end) - new Date(start)) / 60000); return Number.isFinite(minutes) && minutes > 0 ? (minutes >= 60 ? `${Math.floor(minutes / 60)}h${minutes % 60 ? ` ${minutes % 60}min` : ''}` : `${minutes}min`) : ''; };

const agendaCard = {
  mount(el, data, { documentRef: doc }) {
    const list = create(doc, 'ol', 'kui-pres-agenda');
    el.appendChild(list);
    const update = (next = {}) => {
      clear(list);
      const events = Array.isArray(next.events) ? next.events : [];
      events.forEach((event) => {
        const item = create(doc, 'li', `kui-agenda-item${(event?.conflictsWith || []).length ? ' is-conflict' : ''}`);
        item.appendChild(create(doc, 'b', 'kui-pres-when', event?.allDay ? 'dia todo' : `${timeOf(event?.startAt)}–${timeOf(event?.endAt)}`));
        const body = create(doc, 'span', 'kui-agenda-body');
        body.appendChild(create(doc, 'span', 'kui-agenda-title', safeText(event?.title, '(sem título)')));
        const meta = [event?.allDay ? '' : duration(event?.startAt, event?.endAt), safeText(event?.location), (event?.conflictsWith || []).length ? 'conflito de horário' : ''].filter(Boolean).join(' · ');
        if (meta) body.appendChild(create(doc, 'span', 'kui-pres-muted', meta));
        item.appendChild(body);
        list.appendChild(item);
      });
      if (!events.length) list.appendChild(create(doc, 'li', 'kui-pres-muted', `Nenhum compromisso em ${safeText(next.period, 'este período')} (consulta concluída).`));
    };
    update(data);
    return { update, unmount() { clear(list); } };
  },
};

const emailList = {
  mount(el, data, { documentRef: doc, open }) {
    const list = create(doc, 'div', 'kui-pres-emails');
    el.appendChild(list);
    const update = (next = {}) => {
      clear(list);
      (Array.isArray(next.messages) ? next.messages : []).forEach((message) => {
        const url = safeHttps(message?.sourceLink);
        const card = create(doc, 'button', `kui-email-card is-${safeText(message?.priority, 'baixa').replace(/[^a-zé]/g, '')}`);
        card.type = 'button';
        card.disabled = !url;
        card.title = 'Abrir a mensagem original';
        card.addEventListener?.('click', () => open(url));
        card.appendChild(create(doc, 'b', 'kui-email-subject', safeText(message?.subject, '(sem assunto)')));
        card.appendChild(create(doc, 'span', 'kui-pres-muted', [safeText(message?.sender), formatDate(message?.receivedAt), `prioridade ${safeText(message?.priority)}`, message?.deadline ? `prazo ${safeText(message.deadline)}` : ''].filter(Boolean).join(' · ')));
        card.appendChild(create(doc, 'span', 'kui-email-snippet', `Trecho original: ${safeText(message?.snippet)}`));
        list.appendChild(card);
      });
    };
    update(data);
    return { update, unmount() { clear(list); } };
  },
};

const clock = (seconds) => (typeof seconds === 'number' && seconds > 0 ? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}` : '');

const videoCards = {
  mount(el, data, { documentRef: doc, open }) {
    const stage = create(doc, 'div', 'kui-video-stage');
    const list = create(doc, 'div', 'kui-pres-videos');
    el.appendChild(stage);
    el.appendChild(list);
    const player = createVideoPlayer(doc, stage);
    const update = (next = {}) => {
      player.destroy();
      clear(list);
      (Array.isArray(next.videos) ? next.videos : []).slice(0, 4).forEach((video) => {
        const card = create(doc, 'div', 'kui-video-card');
        const src = safeImage(video?.thumbnailUrl);
        if (src) { const image = create(doc, 'img', 'kui-video-thumb'); image.src = src; image.alt = ''; image.loading = 'lazy'; card.appendChild(image); }
        const text = create(doc, 'div', 'kui-news-text');
        text.appendChild(create(doc, 'b', 'kui-news-title', safeText(video?.title, 'Vídeo')));
        text.appendChild(create(doc, 'span', 'kui-pres-muted', [safeText(video?.channel), clock(video?.durationSeconds), video?.publishedAt ? formatDate(video.publishedAt) : ''].filter(Boolean).join(' · ')));
        if (video?.description) text.appendChild(create(doc, 'span', 'kui-news-summary', safeText(video.description)));
        const actions = create(doc, 'div', 'kui-video-actions');
        const playButton = create(doc, 'button', 'kui-panel-button', video?.embeddable ? 'Reproduzir' : 'Não incorporável');
        playButton.type = 'button';
        playButton.disabled = !video?.embeddable;
        playButton.addEventListener?.('click', () => player.play(video?.videoId));
        const openButton = create(doc, 'button', 'kui-panel-button', 'Abrir no navegador');
        openButton.type = 'button';
        openButton.disabled = !safeHttps(video?.url);
        openButton.addEventListener?.('click', () => { player.destroy(); open(video?.url); });
        actions.appendChild(playButton);
        actions.appendChild(openButton);
        text.appendChild(actions);
        card.appendChild(text);
        list.appendChild(card);
      });
    };
    update(data);
    return { update, unmount() { player.destroy(); clear(list); } };
  },
};

const SVG_NS = 'http://www.w3.org/2000/svg';
const KIND_LABEL = { observed: 'observado', forecast: 'previsão', estimate: 'estimativa' };
const formatValue = (value, unit) => (typeof value === 'number' ? `${value.toLocaleString('pt-BR', { maximumFractionDigits: 1 })}${unit ? ` ${unit}` : ''}` : 'sem dado');

function svgNode(doc, tag, attrs = {}) {
  const node = typeof doc.createElementNS === 'function' ? doc.createElementNS(SVG_NS, tag) : doc.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

/** Linha, barras ou rosca; valor ausente vira lacuna (nunca é preenchido) e há sempre a tabela com os mesmos dados. */
const chartPanel = {
  mount(el, data, { documentRef: doc }) {
    const root = create(doc, 'div', 'kui-pres-chart');
    el.appendChild(root);
    const update = (chart = {}) => {
      clear(root);
      const labels = chart.xAxis?.labels || [];
      const series = Array.isArray(chart.series) ? chart.series : [];
      const unit = safeText(chart.yAxis?.unit);
      const values = series.flatMap((s) => s.data.map((p) => p.value)).filter((v) => typeof v === 'number');
      if (!labels.length || !values.length) { root.appendChild(create(doc, 'p', 'kui-pres-muted', 'Sem dados para o gráfico.')); return; }
      const width = 560; const height = 220; const left = 44; const bottom = 28; const top = 10;
      const bars = chart.chartType === 'bar';
      const low = bars ? Math.min(0, ...values) : Math.min(...values);
      const high = Math.max(...values, bars ? 0 : -Infinity);
      const span = high - low || 1;
      const y = (v) => top + (height - top - bottom) * (1 - (v - low) / span);
      const step = (width - left - 8) / labels.length;
      const x = (i) => left + step * (i + 0.5);
      const svg = svgNode(doc, 'svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': safeText(chart.title, 'Gráfico') });
      [low, (low + high) / 2, high].forEach((tick) => {
        svg.appendChild(svgNode(doc, 'line', { x1: left, x2: width - 8, y1: y(tick), y2: y(tick), class: 'kui-chart-grid' }));
        const text = svgNode(doc, 'text', { x: left - 6, y: y(tick) + 4, class: 'kui-chart-tick', 'text-anchor': 'end' });
        text.textContent = formatValue(tick, '');
        svg.appendChild(text);
      });
      labels.forEach((label, i) => {
        const text = svgNode(doc, 'text', { x: x(i), y: height - 8, class: 'kui-chart-tick', 'text-anchor': 'middle' });
        text.textContent = label;
        svg.appendChild(text);
      });
      series.forEach((s, index) => {
        const color = `kui-chart-s${index % 6}`;
        if (bars) {
          const barWidth = (step * 0.7) / series.length;
          s.data.forEach((p, i) => {
            if (typeof p.value !== 'number') return;
            const rect = svgNode(doc, 'rect', { x: x(i) - step * 0.35 + barWidth * index, y: Math.min(y(p.value), y(0)), width: barWidth, height: Math.abs(y(0) - y(p.value)), class: `${color}${p.kind !== 'observed' ? ' is-projected' : ''}` });
            const tip = svgNode(doc, 'title'); tip.textContent = `${s.name} · ${labels[i]}: ${formatValue(p.value, unit)} (${KIND_LABEL[p.kind]})`;
            rect.appendChild(tip);
            svg.appendChild(rect);
          });
          return;
        }
        let path = '';
        s.data.forEach((p, i) => { path += typeof p.value === 'number' ? `${path && s.data[i - 1] && typeof s.data[i - 1].value === 'number' ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.value).toFixed(1)} ` : ''; });
        svg.appendChild(svgNode(doc, 'path', { d: path.trim(), class: `kui-chart-line ${color}${s.data.some((p) => p.kind !== 'observed') ? ' is-projected' : ''}` }));
        s.data.forEach((p, i) => {
          if (typeof p.value !== 'number') return;
          const dot = svgNode(doc, 'circle', { cx: x(i), cy: y(p.value), r: 3.5, class: color, tabindex: 0 });
          const tip = svgNode(doc, 'title'); tip.textContent = `${s.name} · ${labels[i]}: ${formatValue(p.value, unit)} (${KIND_LABEL[p.kind]})`;
          dot.appendChild(tip);
          svg.appendChild(dot);
        });
      });
      root.appendChild(svg);
      const legend = create(doc, 'div', 'kui-chart-legend');
      series.forEach((s, index) => legend.appendChild(create(doc, 'span', `kui-chart-key kui-chart-s${index % 6}`, safeText(s.name))));
      const kinds = [...new Set(series.flatMap((s) => s.data.map((p) => p.kind)))].map((kind) => KIND_LABEL[kind]).join(', ');
      legend.appendChild(create(doc, 'span', 'kui-pres-muted', `${unit ? `${unit} · ` : ''}${kinds}${chart.updatedAt ? ` · ${formatDate(chart.updatedAt)}` : ''}`));
      root.appendChild(legend);
      const table = create(doc, 'table', 'kui-chart-table');
      const head = create(doc, 'tr');
      head.appendChild(create(doc, 'th', '', safeText(chart.xAxis?.label, '')));
      series.forEach((s) => head.appendChild(create(doc, 'th', '', `${safeText(s.name)}${unit ? ` (${unit})` : ''}`)));
      table.appendChild(head);
      labels.forEach((label, i) => {
        const row = create(doc, 'tr');
        row.appendChild(create(doc, 'td', '', label));
        series.forEach((s) => row.appendChild(create(doc, 'td', '', `${formatValue(s.data[i]?.value, '')}${s.data[i]?.kind !== 'observed' && typeof s.data[i]?.value === 'number' ? ` (${KIND_LABEL[s.data[i].kind]})` : ''}`)));
        table.appendChild(row);
      });
      const details = create(doc, 'details', 'kui-chart-details');
      details.appendChild(create(doc, 'summary', '', 'Ver em tabela'));
      details.appendChild(table);
      root.appendChild(details);
    };
    update(data);
    return { update, unmount() { clear(root); } };
  },
};

async function postLocal(fetchRef, path, body) {
  const response = await fetchRef(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body == null ? undefined : JSON.stringify(body) });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(safeText(payload?.detail, 'falhou'));
  return payload;
}

/** Saúde: consentimento explícito, importação de arquivo exportado pelo usuário, resumos sem diagnóstico e "apagar tudo". */
const healthCard = {
  mount(el, data, { documentRef: doc, fetchRef = globalThis.fetch }) {
    const root = create(doc, 'div', 'kui-pres-health');
    el.appendChild(root);
    const note = (text) => { const p = create(doc, 'p', 'kui-pres-muted', text); root.appendChild(p); return p; };
    const update = (next = {}) => {
      clear(root);
      if (!next.consented) {
        root.appendChild(create(doc, 'p', '', safeText(next.scope)));
        root.appendChild(create(doc, 'p', 'kui-pres-muted', 'Nada é lido antes da sua autorização. Você pode revogar e apagar tudo quando quiser.'));
        const allow = create(doc, 'button', 'kui-panel-button', 'Autorizar');
        allow.type = 'button';
        allow.addEventListener?.('click', () => postLocal(fetchRef, '/api/ui/health/consent').catch((error) => note(`Não consegui: ${error.message}`)));
        root.appendChild(allow);
        return;
      }
      (Array.isArray(next.summaries) ? next.summaries : []).forEach((item) => {
        const box = create(doc, 'div', 'kui-health-item');
        box.appendChild(create(doc, 'b', '', safeText(item?.categoria).replace(/_/g, ' ')));
        if (item?.ultimo) box.appendChild(create(doc, 'span', '', `último: ${item.ultimo.valor} ${safeText(item.unidade)} em ${safeText(item.ultimo.data)} (${safeText(item.ultimo.origem)})`));
        if (item?.tendencia) box.appendChild(create(doc, 'span', 'kui-pres-muted', `tendência: ${safeText(item.tendencia)}`));
        if (item?.faixa_de_referencia?.ultimo_fora_da_faixa) box.appendChild(create(doc, 'span', 'kui-news-tag is-warn', 'último valor fora da faixa de referência informada'));
        if (item?.observacao) box.appendChild(create(doc, 'span', 'kui-pres-muted', safeText(item.observacao)));
        root.appendChild(box);
      });
      if (!next.total) note('Nenhum registro importado. Ausência de registro não indica nada sobre a sua saúde.');
      root.appendChild(create(doc, 'p', 'kui-pres-muted', safeText(next.limitations)));
      const input = create(doc, 'input', 'kui-health-file');
      input.type = 'file';
      input.accept = 'application/json,.json';
      input.addEventListener?.('change', async () => {
        const file = input.files?.[0];
        if (!file) return;
        try {
          const parsed = JSON.parse(await file.text());
          const result = await postLocal(fetchRef, '/api/ui/health/import', Array.isArray(parsed) ? { records: parsed } : parsed);
          note(`Importados: ${result.importados}. Rejeitados: ${(result.rejeitados || []).length}.`);
        } catch (error) { note(`Não importei: ${error.message}`); }
      });
      const label = create(doc, 'label', 'kui-panel-button', 'Importar arquivo (JSON)');
      label.appendChild(input);
      root.appendChild(label);
      const wipe = create(doc, 'button', 'kui-panel-button is-danger', 'Revogar e apagar tudo');
      wipe.type = 'button';
      wipe.addEventListener?.('click', () => {
        if (!globalThis.confirm?.('Apagar todos os dados de saúde guardados pela Kira?')) return;
        postLocal(fetchRef, '/api/ui/health/revoke').catch((error) => note(`Não consegui: ${error.message}`));
      });
      root.appendChild(wipe);
    };
    update(data);
    return { update, unmount() { clear(root); } };
  },
};

/**
 * Receita (assistente de cozinha, services/kitchen.py): ingredientes para ticar, passos com o atual em destaque e
 * timers correndo. Os botões mexem no mesmo estado que a voz ("próximo passo"), pelo backend.
 */
const mmss = (ms) => { const total = Math.max(0, Math.round(ms / 1000)); const h = Math.floor(total / 3600); const m = Math.floor((total % 3600) / 60); const s = total % 60; return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`; };

const recipeCard = {
  mount(el, data, { documentRef: doc, fetchRef = globalThis.fetch, windowRef = globalThis.window }) {
    const root = create(doc, 'div', 'kui-recipe');
    el.appendChild(root);
    const checked = new Set();
    let state = {};
    let tick = null;
    const control = (body) => postLocal(fetchRef, '/api/ui/kitchen/control', body).catch(() => {});
    const button = (label, className, action) => {
      const node = create(doc, 'button', `kui-recipe-btn ${className}`, label);
      node.type = 'button';
      node.addEventListener?.('click', action);
      return node;
    };
    const timersBox = create(doc, 'div', 'kui-recipe-timers');
    const paintTimers = () => {
      clear(timersBox);
      const now = Date.now();
      (Array.isArray(state.timers) ? state.timers : []).forEach((timer) => {
        const left = Number(timer?.endsAt || 0) - now;
        const chip = create(doc, 'div', `kui-recipe-timer${left <= 0 ? ' is-done' : ''}`);
        chip.appendChild(create(doc, 'span', 'kui-recipe-timer-label', safeText(timer?.label, 'Timer')));
        chip.appendChild(create(doc, 'b', 'kui-recipe-timer-clock', left > 0 ? mmss(left) : 'pronto!'));
        timersBox.appendChild(chip);
      });
      timersBox.hidden = !timersBox.children?.length && !(state.timers || []).length;
    };
    const update = (next = {}) => {
      state = next || {};
      clear(root);
      const head = create(doc, 'div', 'kui-recipe-head');
      head.appendChild(create(doc, 'b', 'kui-recipe-title', safeText(state.title, 'Receita')));
      const meta = [state.servings ? `${state.servings} porções` : '', state.totalMinutes ? `${state.totalMinutes >= 60 ? `${Math.floor(state.totalMinutes / 60)}h${state.totalMinutes % 60 ? ` ${state.totalMinutes % 60}min` : ''}` : `${state.totalMinutes} min`}` : '', safeText(state.difficulty)].filter(Boolean);
      if (meta.length) head.appendChild(create(doc, 'span', 'kui-recipe-meta', meta.join(' · ')));
      root.appendChild(head);
      root.appendChild(timersBox);
      paintTimers();

      const ingredients = Array.isArray(state.ingredients) ? state.ingredients : [];
      const box = create(doc, 'details', 'kui-recipe-ingredients');
      box.open = !(Number(state.current) > 0);
      box.appendChild(create(doc, 'summary', '', `Ingredientes (${ingredients.length})`));
      const list = create(doc, 'ul', '');
      ingredients.forEach((item, index) => {
        const row = create(doc, 'li', `kui-recipe-ing${checked.has(index) ? ' is-checked' : ''}`, safeText(item));
        row.addEventListener?.('click', () => { if (checked.has(index)) checked.delete(index); else checked.add(index); row.classList.toggle('is-checked', checked.has(index)); });
        list.appendChild(row);
      });
      box.appendChild(list);
      root.appendChild(box);

      const steps = Array.isArray(state.steps) ? state.steps : [];
      const current = Math.max(0, Math.min(steps.length - 1, Number(state.current) || 0));
      const ol = create(doc, 'ol', 'kui-recipe-steps');
      let currentNode = null;
      steps.forEach((step, index) => {
        const li = create(doc, 'li', `kui-recipe-step${index === current ? ' is-current' : index < current ? ' is-done' : ''}`);
        li.appendChild(create(doc, 'span', 'kui-recipe-step-n', String(index + 1)));
        const body = create(doc, 'div', 'kui-recipe-step-body');
        body.appendChild(create(doc, 'p', '', safeText(step?.text)));
        const minutes = Number(step?.minutes) || 0;
        if (minutes > 0) body.appendChild(button(`⏲ Marcar ${minutes} min`, 'is-timer', () => control({ action: 'timer', minutes, label: `Passo ${index + 1}` })));
        li.appendChild(body);
        li.addEventListener?.('click', (event) => { if (!event?.target?.closest?.('button') && index !== current) control({ action: 'ir', step: index + 1 }); });
        ol.appendChild(li);
        if (index === current) currentNode = li;
      });
      root.appendChild(ol);
      const nav = create(doc, 'div', 'kui-recipe-nav');
      const prev = button('‹ Anterior', 'is-prev', () => control({ action: 'anterior' }));
      prev.disabled = current <= 0;
      const nextBtn = button(current >= steps.length - 1 ? 'Último passo' : 'Próximo ›', 'is-next', () => control({ action: 'proximo' }));
      nextBtn.disabled = current >= steps.length - 1;
      nav.append(prev, create(doc, 'span', 'kui-recipe-progress', steps.length ? `${current + 1} / ${steps.length}` : ''), nextBtn);
      root.appendChild(nav);
      (Array.isArray(state.tips) ? state.tips : []).forEach((tip) => root.appendChild(create(doc, 'p', 'kui-recipe-tip', `Dica: ${safeText(tip)}`)));
      windowRef?.requestAnimationFrame?.(() => currentNode?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' }));
    };
    update(data);
    tick = windowRef?.setInterval?.(paintTimers, 1000);
    return { update, unmount() { windowRef?.clearInterval?.(tick); clear(root); } };
  },
};

/**
 * Instagram: o perfil num celular no meio e os balões em volta (+3 seguidores, -1, mensagem de fulano, curtidas),
 * cada um com um ponto e uma linha que encosta no celular. Tela estreita: os balões descem para baixo do celular.
 */
const instagramCard = {
  mount(el, data, { documentRef: doc }) {
    const root = create(doc, 'div', 'kui-insta');
    el.appendChild(root);
    const avatar = (src, className) => {
      const url = safeImage(src);
      const node = create(doc, url ? 'img' : 'span', className);
      if (url) { node.src = url; node.alt = ''; node.loading = 'lazy'; node.referrerPolicy = 'no-referrer'; }
      return node;
    };
    const count = (n) => (Number(n) >= 10000 ? `${(Number(n) / 1000).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} mil` : Number(n || 0).toLocaleString('pt-BR'));
    function update(next) {
      clear(root);
      const state = next || {};
      const p = state.profile || {};
      const callouts = Array.isArray(state.callouts) ? state.callouts.slice(0, 8) : [];
      const side = (items, where) => {
        const col = create(doc, 'div', `kui-insta-side is-${where}`);
        items.forEach((item) => {
          const bubble = create(doc, 'div', `kui-insta-callout tone-${['good', 'bad', 'info', 'calm'].includes(item?.tone) ? item.tone : 'info'}`);
          bubble.appendChild(create(doc, 'b', 'kui-insta-callout-value', safeText(item?.value, '')));
          bubble.appendChild(create(doc, 'span', 'kui-insta-callout-label', safeText(item?.label, '')));
          if (item?.text) bubble.appendChild(create(doc, 'small', 'kui-insta-callout-text', safeText(item.text)));
          col.appendChild(bubble);
        });
        return col;
      };
      const half = Math.ceil(callouts.length / 2);
      const phone = create(doc, 'div', 'kui-insta-phone');
      const screen = create(doc, 'div', 'kui-insta-screen');
      phone.append(create(doc, 'span', 'kui-insta-notch'), screen);
      const head = create(doc, 'div', 'kui-insta-head');
      head.appendChild(avatar(p.photoUrl, 'kui-insta-avatar'));
      const who = create(doc, 'div', 'kui-insta-who');
      who.appendChild(create(doc, 'b', '', `@${safeText(p.username, 'instagram')}${p.verified ? ' ✔' : ''}`));
      if (p.name) who.appendChild(create(doc, 'span', '', safeText(p.name)));
      head.appendChild(who);
      screen.appendChild(head);
      const stats = create(doc, 'div', 'kui-insta-stats');
      [['posts', p.posts], ['seguidores', p.followers], ['seguindo', p.following]].forEach(([label, value]) => {
        const cell = create(doc, 'div', '');
        cell.append(create(doc, 'b', '', count(value)), create(doc, 'span', '', label));
        stats.appendChild(cell);
      });
      screen.appendChild(stats);
      const messages = Array.isArray(state.messages) ? state.messages : [];
      if (messages.length) {
        screen.appendChild(create(doc, 'div', 'kui-insta-title', 'Mensagens de hoje'));
        messages.forEach((m) => {
          const row = create(doc, 'div', `kui-insta-msg${m?.unread ? ' is-unread' : ''}`);
          row.append(create(doc, 'b', '', safeText(m?.name, '')), create(doc, 'span', '', safeText(m?.text, '')), create(doc, 'small', '', safeText(m?.time, '')));
          screen.appendChild(row);
        });
      }
      const people = Array.isArray(state.people) ? state.people : [];
      if (people.length) {
        screen.appendChild(create(doc, 'div', 'kui-insta-title', safeText(state.peopleTitle, 'Contas')));
        people.forEach((u) => {
          const row = create(doc, 'button', 'kui-insta-person');
          row.type = 'button';
          row.append(avatar(u?.photoUrl, 'kui-insta-mini'), create(doc, 'span', '', `@${safeText(u?.username, '')}${u?.verified ? ' ✔' : ''}`), create(doc, 'small', '', safeText(u?.name, '')));
          const link = safeHttps(u?.profileUrl);
          if (link) row.addEventListener('click', () => openExternal(link));
          screen.appendChild(row);
        });
      }
      if (state.note) screen.appendChild(create(doc, 'p', 'kui-insta-note', safeText(state.note)));
      root.append(side(callouts.slice(0, half), 'left'), phone, side(callouts.slice(half), 'right'));
    }
    update(data);
    return { update, unmount() { clear(root); } };
  },
};

export const COMPONENTS = new Map([
  ['recipe', recipeCard],
  ['instagram', instagramCard],
  ['health', healthCard],
  ['chart', chartPanel],
  ['agenda', agendaCard],
  ['video', videoCards],
  ['email', emailList],
  ['weather', weatherCard],
  ['news', newsCards],
  ['text', textCard],
  ['sources', sourceList],
  ['timeline', timeline],
  ['image_gallery', imageGallery],
]);

/** Fases seguintes registram aqui os componentes de clima, notícias, eleição, gráfico, vídeo etc. */
export function registerComponent(type, component) {
  if (!type || typeof component?.mount !== 'function') throw new TypeError('componente inválido');
  COMPONENTS.set(type, component);
}
