import { el, clear, timeParts, applyAppearance } from './kit.js';

const WEEK = ['D', 'S', 'T', 'Q', 'Q', 'S', 'S'];

function monthGrid(year, month, today, { compactHead = false } = {}) {
  const grid = el('div', 'w-cal-grid');
  WEEK.forEach((d) => grid.appendChild(el('span', 'w-cal-head', d)));
  const first = new Date(year, month, 1).getDay();
  const days = new Date(year, month + 1, 0).getDate();
  for (let i = 0; i < first; i += 1) grid.appendChild(el('span'));
  for (let d = 1; d <= days; d += 1) {
    const isToday = today && today.getFullYear() === year && today.getMonth() === month && today.getDate() === d;
    grid.appendChild(el('span', isToday ? 'w-cal-day is-today' : 'w-cal-day', String(d)));
  }
  if (compactHead) grid.classList.add('is-compact');
  return grid;
}

const STYLES = [['formal', 'Mês formal'], ['minimal', 'Mês minimalista'], ['tech', 'Mês tech (neon)'], ['bigday', 'Só números (dia grande)'], ['week', 'Faixa semanal'], ['agenda', 'Agenda com lembretes'], ['year', 'Ano compacto']];

const calendar = {
  kind: 'calendar', title: 'Calendário', category: 'Tempo', size: { w: 300, h: 280 }, bare: true, styles: STYLES,
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const style = ctx.style || 'formal';
    box.classList.add('w-calendar', `w-cal-${style}`);
    const body = el('div', 'w-cal-body');
    box.appendChild(body);
    const draw = () => {
      const now = new Date();
      clear(body);
      if (style === 'bigday') {
        const p = timeParts(now);
        body.append(el('div', 'w-bigday', p.day), el('div', 'w-center', `${p.weekday} · ${p.month}`));
      } else if (style === 'week') {
        const strip = el('div', 'w-cal-week');
        for (let i = -3; i <= 3; i += 1) {
          const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i);
          const cell = el('div', i === 0 ? 'w-cal-wday is-today' : 'w-cal-wday');
          cell.append(el('span', '', WEEK[day.getDay()]), el('b', '', String(day.getDate())));
          strip.appendChild(cell);
        }
        body.append(el('div', 'w-cal-title', now.toLocaleDateString('pt-BR', { month: 'long', year: 'numeric' })), strip);
      } else if (style === 'year') {
        const year = el('div', 'w-cal-year');
        for (let m = 0; m < 12; m += 1) {
          const cell = el('div', 'w-cal-mini');
          cell.append(el('div', 'w-cal-mini-title', new Date(now.getFullYear(), m, 1).toLocaleDateString('pt-BR', { month: 'short' })), monthGrid(now.getFullYear(), m, now, { compactHead: true }));
          year.appendChild(cell);
        }
        body.appendChild(year);
      } else if (style === 'agenda') {
        body.appendChild(el('div', 'w-cal-title', now.toLocaleDateString('pt-BR', { weekday: 'long', day: '2-digit', month: 'long' })));
        const list = el('div', 'w-list');
        body.appendChild(list);
        fetch('/api/reminders', { cache: 'no-store' }).then((r) => r.json()).then((data) => {
          const items = (data.items || []).filter((i) => i.status === 'scheduled').sort((a, b) => String(a.due_at).localeCompare(String(b.due_at))).slice(0, 5);
          if (!items.length) { list.appendChild(el('p', 'w-muted w-center', 'nenhum lembrete agendado')); return; }
          items.forEach((item) => {
            const row = el('div', 'w-row');
            row.append(el('span', '', item.text), el('span', 'w-num', new Date(item.due_at).toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })));
            list.appendChild(row);
          });
        }).catch(() => list.appendChild(el('p', 'w-muted w-center', 'indisponível')));
      } else {
        body.append(el('div', 'w-cal-title', now.toLocaleDateString('pt-BR', { month: 'long', year: 'numeric' })), monthGrid(now.getFullYear(), now.getMonth(), now));
      }
    };
    draw();
    ctx.every(style === 'agenda' ? 30000 : 60000, draw);
  },
};

export const CALENDAR_WIDGETS = [calendar];
