import { el, clear, setText, unavailable, applyAppearance } from './kit.js';
import { sanitizeIdentitySnapshot, presentIdentityEmail } from '../apiIdentityPresentation.js';
import { createViewerPanel } from '../views/ViewerPanel.js';

const MAX_LINES = 80;

const transcript = {
  kind: 'transcript', title: 'Transcrição', category: 'Kira', size: { w: 380, h: 320 }, bare: false,
  styles: [['bubbles', 'Bolhas (como o painel da Kira)'], ['compact', 'Compacta'], ['last', 'Só a última fala'], ['karaoke', 'Legenda grande (karaokê)']],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const style = ctx.style || 'bubbles';
    const single = style === 'last' || style === 'karaoke';
    box.classList.add('w-transcript', `w-tr-${style}`);
    const lines = el('div', 'w-lines');
    box.appendChild(lines);
    const label = (role) => (role === 'user' ? 'VOCÊ' : 'KIRA');
    const add = ({ role, text }) => {
      if (single) {
        clear(lines);
        const line = el('div', `w-line w-last is-${role}`);
        line.dataset.role = role;
        line.append(el('b', '', label(role)), el('span', 'w-last-text', text));
        lines.appendChild(line);
        return;
      }
      const row = el('div', `w-line is-${role}`);
      row.dataset.role = role;
      row.append(el('b', 'w-who', `${label(role)}:`), el('span', 'w-text', text));
      lines.appendChild(row);
      while (lines.childElementCount > MAX_LINES) lines.firstElementChild.remove();
      lines.scrollTop = lines.scrollHeight;
    };
    const buffer = single ? ctx.transcriptBuffer.slice(-1) : ctx.transcriptBuffer;
    buffer.forEach(add);
    if (!buffer.length && single) lines.appendChild(el('p', 'w-muted w-center', 'aguardando a conversa…'));
    return ctx.on('transcript', add);
  },
};

const reminders = {
  kind: 'reminders', title: 'Próximos lembretes', category: 'Kira', size: { w: 340, h: 260 }, bare: false,
  styles: [['list', 'Lista'], ['cards', 'Cartões']],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    box.classList.add('w-reminders', `w-rem-${ctx.style || 'list'}`);
    const body = el('div', ctx.style === 'cards' ? 'w-cards' : 'w-list');
    box.appendChild(body);
    const when = (iso) => new Date(iso).toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
    const refresh = async () => {
      try {
        const data = await (await fetch('/api/reminders', { cache: 'no-store' })).json();
        const items = (data.items || []).filter((i) => i.status === 'scheduled').sort((a, b) => String(a.due_at).localeCompare(String(b.due_at))).slice(0, 6);
        clear(body);
        if (!items.length) { body.appendChild(el('p', 'w-muted w-center', 'nenhum lembrete agendado')); return; }
        items.forEach((item) => {
          if (ctx.style === 'cards') {
            const card = el('div', 'w-reminder-card');
            card.append(el('b', '', when(item.due_at)), el('span', '', item.text));
            body.appendChild(card);
          } else {
            const row = el('div', 'w-row');
            row.append(el('span', '', item.text), el('span', 'w-num', when(item.due_at)));
            body.appendChild(row);
          }
        });
      } catch (_) { if (!body.firstChild) unavailable(body); }
    };
    refresh();
    ctx.every(15000, refresh);
  },
};

const coverOf = (track, data) => {
  const id = track.videoId || data.videoId || '';
  const raw = track.thumbnail || track.cover || (id ? `https://i.ytimg.com/vi/${id}/hqdefault.jpg` : '');
  return /^https:\/\//.test(raw) ? `/api/ui/img?u=${encodeURIComponent(raw)}` : '';
};

const music = {
  kind: 'music', title: 'Música', category: 'Kira', size: { w: 320, h: 160 }, bare: true,
  styles: [['compact', 'Compacto'], ['cover', 'Capa grande'], ['mini', 'Mini player'], ['visualizer', 'Barra com visualizador']],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const style = ctx.style || 'compact';
    box.classList.add('w-music', `w-music-${style}`);
    const cover = el('img', 'w-music-cover');
    cover.alt = '';
    cover.hidden = true;
    cover.referrerPolicy = 'no-referrer';
    cover.addEventListener('error', () => { cover.hidden = true; });
    const title = el('div', 'w-music-title', 'Nada tocando');
    const sub = el('div', 'w-muted w-music-sub');
    const bars = el('div', 'w-eq');
    for (let i = 0; i < 5; i += 1) bars.appendChild(el('span'));
    if (style === 'cover' || style === 'mini') box.appendChild(cover);
    const text = el('div', 'w-music-text');
    text.append(title, sub);
    box.appendChild(text);
    if (style === 'visualizer') box.appendChild(bars);
    const refresh = async () => {
      try {
        const data = (await (await fetch('/api/music/state?active=true', { cache: 'no-store' })).json()).data || {};
        const track = data.track || data.currentTrack || data.current || {};
        const name = track.title || data.title || data.trackTitle || '';
        const playing = Boolean(data.isPlaying && name);
        setText(title, playing ? name : 'Nada tocando');
        setText(sub, playing ? String(track.artist || track.channel || '') : 'Peça uma música à Kira');
        box.classList.toggle('is-playing', playing);
        const url = playing ? coverOf(track, data) : '';
        cover.hidden = !url;
        if (url && cover.getAttribute('src') !== url) cover.src = url;
      } catch (_) { setText(title, 'indisponível'); }
    };
    refresh();
    ctx.every(5000, refresh);
  },
};

const apis = {
  kind: 'apis', title: 'APIs ativas', category: 'Kira', size: { w: 300, h: 200 }, bare: false,
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    box.classList.add('w-apis');
    box.appendChild(el('div', 'w-apis-title', 'APIS ATIVAS'));
    const list = el('div', 'w-apis-list');
    box.appendChild(list);
    const refresh = async () => {
      try {
        const safe = sanitizeIdentitySnapshot(await (await fetch('/api/api-identity/snapshot', { cache: 'no-store' })).json());
        clear(list);
        [['livekit', 'LIVEKIT'], ['gemini', 'GEMINI'], ['internet', 'INTERNET']].filter(([key]) => safe[key]).forEach(([key, label]) => {
          const item = safe[key];
          const row = el('div', 'w-api-row');
          const dot = el('span', 'w-dot');
          dot.dataset.state = item.status === 'online' ? 'ready' : item.status === 'warning' ? 'connecting' : 'error';
          const info = el('span', 'w-api-info');
          info.append(el('b', '', `API ${item.slot ?? item.active_slot ?? '?'}/${item.total ?? item.total_slots ?? '?'}${item.kind === 'cloud' ? ' — Cloud' : ''}`), el('span', 'w-muted', presentIdentityEmail(item.email).masked));
          row.append(el('span', 'w-api-name', label), dot, info);
          list.appendChild(row);
        });
      } catch (_) { if (!list.firstChild) unavailable(list); }
    };
    refresh();
    ctx.every(5000, refresh);
  },
};

const viewer = {
  kind: 'viewer', title: 'Visualizador de imagens', category: 'Kira', size: { w: 520, h: 400 }, bare: false,
  mount(box, ctx) {
    const definition = createViewerPanel();
    const unmount = definition.mount(box, { id: 'viewer-widget', props: {} });
    let viewerId = '';
    const unsubscribe = ctx.on('action', (action) => {
      if (action.type === 'open_panel' && action.kind === 'viewer') viewerId = action.id || 'viewer';
      else if (!(action.type === 'update_panel' && action.id === viewerId)) return;
      definition.update(box, action.props || {});
    });
    return () => { unsubscribe(); unmount?.(); };
  },
};

export const KIRA_WIDGETS = [transcript, reminders, music, apis, viewer];
