import { el, clear, setText, timeParts, formatClock, formatDuration, pad, svg, applyAppearance } from './kit.js';

const DOT_FONT = {
  0: '111101101101111', 1: '010110010010111', 2: '111001111100111', 3: '111001111001111', 4: '101101111001001',
  5: '111100111001111', 6: '111100111101111', 7: '111001001001001', 8: '111101111101111', 9: '111101111001111', ':': '000010000010000',
};
const SEGMENTS = {
  0: 'abcdef', 1: 'bc', 2: 'abged', 3: 'abgcd', 4: 'fgbc', 5: 'afgcd', 6: 'afgedc', 7: 'abc', 8: 'abcdefg', 9: 'abcdfg',
};
const SEG_POINTS = {
  a: '6,2 34,2 30,8 10,8', b: '36,4 36,34 30,30 30,10', c: '36,38 36,68 30,62 30,42', d: '6,70 34,70 30,64 10,64',
  e: '4,38 4,68 10,62 10,42', f: '4,4 4,34 10,30 10,10', g: '6,36 34,36 30,41 10,41',
};
const ROMAN = ['XII', 'I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X', 'XI'];
const UNITS = ['zero', 'um', 'dois', 'três', 'quatro', 'cinco', 'seis', 'sete', 'oito', 'nove', 'dez', 'onze', 'doze', 'treze', 'catorze', 'quinze', 'dezesseis', 'dezessete', 'dezoito', 'dezenove'];
const TENS = { 20: 'vinte', 30: 'trinta', 40: 'quarenta', 50: 'cinquenta' };
const DEFAULT_ZONES = 'America/Sao_Paulo,Europe/London,Asia/Tokyo,America/New_York';

/** Número 0–59 por extenso ("quarenta e cinco"). */
export function numberWords(value) {
  if (value < 20) return UNITS[value];
  const unit = value % 10;
  return unit ? `${TENS[value - unit]} e ${UNITS[unit]}` : TENS[value];
}

/** "Sete e quarenta", "Meia-noite e quinze", "Oito em ponto". */
export function timeInWords(parts) {
  const hour = parts.h === 0 ? 'meia-noite' : parts.h === 12 ? 'meio-dia' : parts.h === 1 || parts.h === 13 ? 'uma' : numberWords(parts.h % 12 || 12);
  const text = parts.m === 0 ? `${hour} em ponto` : `${hour} e ${numberWords(parts.m)}`;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function sevenSegment(text) {
  const glyphs = [...text];
  const width = glyphs.reduce((total, ch) => total + (ch === ':' ? 16 : 44), 0);
  const root = svg('svg', { viewBox: `0 0 ${width} 72`, class: 'w-seven' });
  const nodes = [];
  let x = 0;
  glyphs.forEach((ch) => {
    const group = svg('g', { transform: `translate(${x} 0)` }, root);
    if (ch === ':') {
      svg('circle', { cx: 8, cy: 24, r: 3, class: 'on' }, group);
      svg('circle', { cx: 8, cy: 50, r: 3, class: 'on' }, group);
      x += 16;
    } else {
      const segs = {};
      Object.entries(SEG_POINTS).forEach(([key, points]) => { segs[key] = svg('polygon', { points, class: 'seg' }, group); });
      nodes.push(segs);
      x += 44;
    }
  });
  return {
    root,
    set(value) {
      let index = 0;
      [...value].forEach((ch) => {
        if (ch === ':') return;
        const lit = SEGMENTS[ch] || '';
        Object.entries(nodes[index] || {}).forEach(([key, node]) => node.classList.toggle('on', lit.includes(key)));
        index += 1;
      });
    },
  };
}

function dottedText(text) {
  const cell = 7;
  const glyphs = [...text].filter((ch) => DOT_FONT[ch]);
  const root = svg('svg', { viewBox: `0 0 ${glyphs.length * 4 * cell} ${5 * cell}`, class: 'w-dotted' });
  glyphs.forEach((ch, index) => {
    [...DOT_FONT[ch]].forEach((bit, i) => {
      if (bit === '1') svg('circle', { cx: (index * 4 + (i % 3)) * cell + cell / 2, cy: Math.floor(i / 3) * cell + cell / 2, r: 2.6 }, root);
    });
  });
  return root;
}

function analogFace(variant) {
  const root = svg('svg', { viewBox: '-100 -100 200 200', class: 'w-analog' });
  svg('circle', { r: 94, class: 'w-face' }, root);
  if (variant === 'tech') {
    [86, 70, 54].forEach((r, i) => svg('circle', { r, class: `w-ring w-ring-${i}` }, root));
  }
  for (let i = 0; i < (variant === 'tech' ? 60 : 12); i += 1) {
    const step = variant === 'tech' ? 6 : 30;
    const angle = (i * step * Math.PI) / 180;
    if (variant === 'roman') {
      const label = svg('text', { x: Math.sin(angle) * 74, y: -Math.cos(angle) * 74 + 5, class: 'w-roman', 'text-anchor': 'middle' }, root);
      label.textContent = ROMAN[i];
    } else {
      const long = variant === 'classic' ? 10 : variant === 'tech' ? (i % 5 === 0 ? 8 : 3) : 6;
      svg('line', { x1: Math.sin(angle) * 90, y1: -Math.cos(angle) * 90, x2: Math.sin(angle) * (90 - long), y2: -Math.cos(angle) * (90 - long), class: 'w-tick' }, root);
    }
  }
  const hand = (cls, length) => svg('line', { x1: 0, y1: 6, x2: 0, y2: -length, class: cls }, root);
  const hands = { h: hand('w-hand w-hand-h', 48), m: hand('w-hand w-hand-m', 70), s: hand('w-hand w-hand-s', 80) };
  svg('circle', { r: 4, class: 'w-pin' }, root);
  return {
    root,
    set(parts, withSeconds) {
      hands.h.setAttribute('transform', `rotate(${(parts.h % 12) * 30 + parts.m * 0.5})`);
      hands.m.setAttribute('transform', `rotate(${parts.m * 6 + parts.s * 0.1})`);
      hands.s.setAttribute('transform', `rotate(${parts.s * 6})`);
      hands.s.style.display = withSeconds ? '' : 'none';
    },
  };
}

function dotRing() {
  const root = svg('svg', { viewBox: '-100 -100 200 200', class: 'w-dotring' });
  const dots = [];
  for (let i = 0; i < 60; i += 1) {
    const angle = (i * 6 * Math.PI) / 180;
    dots.push(svg('circle', { cx: Math.sin(angle) * 86, cy: -Math.cos(angle) * 86, r: i % 5 === 0 ? 3.4 : 1.8 }, root));
  }
  const label = svg('text', { x: 0, y: 8, 'text-anchor': 'middle', class: 'w-ring-text' }, root);
  return {
    root,
    set(parts, withSeconds, text) {
      dots.forEach((dot, i) => {
        dot.classList.toggle('is-sec', withSeconds && i === parts.s);
        dot.classList.toggle('is-min', i === parts.m);
        dot.classList.toggle('is-hour', i === Math.floor(((parts.h % 12) * 60 + parts.m) / 12) % 60);
      });
      label.textContent = text;
    },
  };
}

function binaryGrid() {
  const grid = el('div', 'w-binary');
  const columns = [];
  for (let c = 0; c < 6; c += 1) {
    const column = el('div', 'w-bit-col');
    const bits = [];
    for (let r = 0; r < 4; r += 1) { const bit = el('span', 'w-bit'); column.appendChild(bit); bits.push(bit); }
    columns.push(bits);
    grid.appendChild(column);
  }
  return {
    grid,
    set(parts) {
      [Math.floor(parts.h / 10), parts.h % 10, Math.floor(parts.m / 10), parts.m % 10, Math.floor(parts.s / 10), parts.s % 10]
        .forEach((digit, c) => columns[c].forEach((bit, r) => bit.classList.toggle('is-on', Boolean(digit & (8 >> r)))));
    },
  };
}

function flipClock(seconds) {
  const wrap = el('div', 'w-flip');
  const cards = [];
  const groups = seconds ? 3 : 2;
  for (let g = 0; g < groups; g += 1) {
    const group = el('div', 'w-flip-group');
    for (let i = 0; i < 2; i += 1) { const card = el('span', 'w-flip-card', '0'); group.appendChild(card); cards.push(card); }
    wrap.appendChild(group);
  }
  return {
    wrap,
    set(parts, hour12) {
      const hour = hour12 ? (parts.h % 12) || 12 : parts.h;
      const digits = `${pad(hour)}${pad(parts.m)}${seconds ? pad(parts.s) : ''}`;
      [...digits].forEach((digit, i) => {
        if (cards[i].textContent === digit) return;
        cards[i].textContent = digit;
        cards[i].classList.remove('is-flip');
        void cards[i].offsetWidth;
        cards[i].classList.add('is-flip');
      });
    },
  };
}

const CLOCK_STYLES = [
  ['neon', 'Digital 7 segmentos neon'], ['thin', 'Digital fino'], ['bigsec', 'Grande com segundos pequenos'], ['flip', 'Flip clock'],
  ['classic', 'Analógico clássico'], ['analog-minimal', 'Analógico minimalista'], ['roman', 'Analógico romano'], ['tech', 'Analógico tech (HUD)'],
  ['dotring', 'Anel de pontos'], ['matrix', 'Pontilhado'], ['binary', 'Binário'], ['daybar', 'Barra do dia'], ['words', 'Por extenso'], ['withdate', 'Relógio com data'],
];

const clockWidget = {
  kind: 'clock', title: 'Relógio', category: 'Tempo', size: { w: 300, h: 200 }, bare: true,
  styles: CLOCK_STYLES,
  optionDefs: [{ key: 'hour12', label: '12 horas', type: 'bool' }, { key: 'seconds', label: 'Segundos', type: 'bool', default: true }, { key: 'tz', label: 'Fuso (ex.: America/Sao_Paulo)', type: 'text' }],
  mount(box, ctx) {
    const style = ctx.style || 'neon';
    const { hour12 = false, seconds = true, tz = '' } = ctx.options;
    box.classList.add('w-clock', `w-clock-${style}`);
    applyAppearance(box, ctx.options);
    const date = el('div', 'w-date');
    let paint;
    if (style === 'classic' || style === 'analog-minimal' || style === 'roman' || style === 'tech') {
      const face = analogFace(style === 'analog-minimal' ? 'minimal' : style);
      box.append(face.root);
      if (style === 'tech') box.appendChild(date);
      paint = (p) => face.set(p, seconds);
    } else if (style === 'dotring') {
      const ring = dotRing();
      box.append(ring.root);
      paint = (p) => ring.set(p, seconds, formatClock(p, { hour12, seconds: false }));
    } else if (style === 'binary') {
      const binary = binaryGrid();
      const label = el('div', 'w-muted w-center');
      box.append(binary.grid, label, date);
      paint = (p) => { binary.set(p); setText(label, formatClock(p, { seconds: true })); };
    } else if (style === 'matrix') {
      const holder = el('div', 'w-dotted-holder');
      box.append(holder, date);
      let last = '';
      paint = (p) => { const text = formatClock(p, { hour12: false, seconds }); if (text !== last) { last = text; clear(holder); holder.appendChild(dottedText(text)); } };
    } else if (style === 'daybar') {
      const time = el('div', 'w-time');
      const bar = el('div', 'w-daybar');
      const fill = el('span');
      bar.appendChild(fill);
      const pct = el('div', 'w-muted w-center');
      box.append(time, bar, pct, date);
      paint = (p) => { setText(time, formatClock(p, { hour12, seconds })); const fraction = (p.h * 3600 + p.m * 60 + p.s) / 86400; fill.style.transform = `scaleX(${fraction.toFixed(4)})`; setText(pct, `${Math.round(fraction * 100)}% do dia`); };
    } else if (style === 'neon') {
      const seg = sevenSegment(formatClock({ h: 0, m: 0, s: 0 }, { seconds }));
      box.append(seg.root, date);
      paint = (p) => seg.set(formatClock(p, { hour12: false, seconds }));
    } else if (style === 'flip') {
      const flip = flipClock(seconds);
      box.append(flip.wrap, date);
      paint = (p) => flip.set(p, hour12);
    } else if (style === 'bigsec') {
      const row = el('div', 'w-bigsec');
      const main = el('span', 'w-time');
      const small = el('span', 'w-bigsec-s');
      row.append(main, small);
      box.append(row, date);
      paint = (p) => { setText(main, formatClock(p, { hour12, seconds: false })); setText(small, seconds ? pad(p.s) : ''); };
    } else if (style === 'words') {
      const words = el('div', 'w-words');
      box.append(words, date);
      paint = (p) => setText(words, timeInWords(p));
    } else if (style === 'withdate') {
      const day = el('div', 'w-withdate-day');
      const time = el('div', 'w-time');
      box.append(day, time, date);
      paint = (p) => { setText(day, `${p.day} ${p.month}`); setText(time, formatClock(p, { hour12, seconds })); };
    } else {
      const time = el('div', 'w-time w-thin');
      box.append(time, date);
      paint = (p) => setText(time, formatClock(p, { hour12, seconds }));
    }
    const tick = () => { const parts = timeParts(new Date(), tz); paint(parts); setText(date, `${parts.weekday}, ${parts.day} de ${parts.month}`); };
    tick();
    ctx.every(1000, tick);
  },
};

const worldClock = {
  kind: 'worldclock', title: 'Relógio mundial', category: 'Tempo', size: { w: 320, h: 240 }, bare: true,
  styles: [['list', 'Lista'], ['cards', 'Cartões']],
  optionDefs: [{ key: 'zones', label: 'Fusos separados por vírgula', type: 'text', default: DEFAULT_ZONES }],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const zones = String(ctx.options.zones || DEFAULT_ZONES).split(',').map((z) => z.trim()).filter(Boolean).slice(0, 8);
    box.classList.add(ctx.style === 'cards' ? 'w-world-cards' : 'w-world-list');
    const rows = zones.map((zone) => {
      const row = el('div', 'w-row');
      const value = el('span', 'w-num');
      row.append(el('span', '', zone.split('/').pop().replace(/_/g, ' ')), value);
      box.appendChild(row);
      return { zone, value };
    });
    const tick = () => rows.forEach((row) => setText(row.value, formatClock(timeParts(new Date(), row.zone), { seconds: false })));
    tick();
    ctx.every(5000, tick);
  },
};

const bigDate = {
  kind: 'bigdate', title: 'Data grande', category: 'Tempo', size: { w: 260, h: 220 }, bare: true,
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const day = el('div', 'w-bigday');
    const rest = el('div', 'w-center');
    box.append(day, rest);
    const draw = () => { const p = timeParts(); setText(day, p.day); setText(rest, `${p.weekday} · ${p.month} ${p.year}`); };
    draw();
    ctx.every(30000, draw);
  },
};

function controls(box, buttons) {
  const bar = el('div', 'w-controls');
  const made = {};
  buttons.forEach(([key, label, handler]) => {
    const button = el('button', 'w-button', label);
    button.type = 'button';
    button.addEventListener('click', handler);
    bar.appendChild(button);
    made[key] = button;
  });
  box.appendChild(bar);
  return made;
}

/** Contagem regressiva visual: anel, barra ou dígitos. */
function countdownDisplay(box, style) {
  const display = el('div', 'w-time');
  if (style === 'ring') {
    const root = svg('svg', { viewBox: '-60 -60 120 120', class: 'w-ring-timer' });
    svg('circle', { r: 50, class: 'w-ring-track' }, root);
    const arc = svg('circle', { r: 50, class: 'w-ring-arc', 'stroke-dasharray': '314.16', 'stroke-dashoffset': '0', transform: 'rotate(-90)' }, root);
    const label = svg('text', { x: 0, y: 7, 'text-anchor': 'middle', class: 'w-ring-text' }, root);
    box.appendChild(root);
    return { set(left, total) { arc.setAttribute('stroke-dashoffset', String(314.16 * (1 - (total ? left / total : 0)))); label.textContent = formatDuration(left); }, done(flag) { root.classList.toggle('is-done', flag); } };
  }
  if (style === 'bar') {
    const bar = el('div', 'w-daybar');
    const fill = el('span');
    bar.appendChild(fill);
    box.append(display, bar);
    return { set(left, total) { setText(display, formatDuration(left)); fill.style.transform = `scaleX(${total ? (left / total).toFixed(4) : 0})`; }, done(flag) { display.classList.toggle('is-done', flag); } };
  }
  box.appendChild(display);
  return { set(left) { setText(display, formatDuration(left)); }, done(flag) { display.classList.toggle('is-done', flag); } };
}

const timer = {
  kind: 'timer', title: 'Timer', category: 'Tempo', size: { w: 260, h: 240 }, bare: true,
  styles: [['ring', 'Circular'], ['bar', 'Barra'], ['digital', 'Digital']],
  optionDefs: [{ key: 'minutes', label: 'Minutos', type: 'number', default: 5 }],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const total = Math.max(1, Number(ctx.options.minutes) || 5) * 60;
    let left = total;
    let endsAt = 0;
    const view = countdownDisplay(box, ctx.style || 'ring');
    const paint = () => {
      if (endsAt) left = Math.max(0, Math.round((endsAt - Date.now()) / 1000));
      view.set(left, total);
      view.done(left === 0);
      if (left === 0) endsAt = 0;
    };
    const buttons = controls(box, [
      ['go', 'Iniciar', () => { if (endsAt) { left = Math.round((endsAt - Date.now()) / 1000); endsAt = 0; } else if (left > 0) endsAt = Date.now() + left * 1000; buttons.go.textContent = endsAt ? 'Pausar' : 'Iniciar'; paint(); }],
      ['reset', 'Zerar', () => { endsAt = 0; left = total; buttons.go.textContent = 'Iniciar'; paint(); }],
    ]);
    paint();
    ctx.every(250, () => { if (endsAt) { paint(); if (!endsAt) buttons.go.textContent = 'Iniciar'; } });
  },
};

const countdown = {
  kind: 'countdown', title: 'Contagem regressiva', category: 'Tempo', size: { w: 320, h: 200 }, bare: true,
  styles: [['blocks', 'Blocos'], ['compact', 'Compacto'], ['big', 'Dias grandes']],
  optionDefs: [{ key: 'label', label: 'Nome do evento', type: 'text', default: 'Evento' }, { key: 'target', label: 'Data e hora', type: 'datetime-local' }],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const style = ctx.style || 'blocks';
    box.classList.add(`w-countdown-${style}`);
    const label = el('div', 'w-muted w-center', ctx.options.label || 'Evento');
    box.appendChild(label);
    const target = ctx.options.target ? new Date(ctx.options.target).getTime() : NaN;
    const cells = {};
    const holder = el('div', style === 'blocks' ? 'w-cd-blocks' : 'w-time');
    box.appendChild(holder);
    if (style === 'blocks') {
      [['d', 'dias'], ['h', 'horas'], ['m', 'min'], ['s', 'seg']].forEach(([key, name]) => {
        const cell = el('div', 'w-cd-cell');
        cells[key] = el('b', '', '--');
        cell.append(cells[key], el('span', '', name));
        holder.appendChild(cell);
      });
    }
    const paint = () => {
      if (!Number.isFinite(target)) { if (style === 'blocks') Object.values(cells).forEach((c) => setText(c, '--')); else setText(holder, 'defina a data'); return; }
      const seconds = Math.max(0, Math.round((target - Date.now()) / 1000));
      const parts = { d: Math.floor(seconds / 86400), h: Math.floor((seconds % 86400) / 3600), m: Math.floor((seconds % 3600) / 60), s: seconds % 60 };
      if (style === 'blocks') Object.entries(parts).forEach(([key, value]) => setText(cells[key], pad(value)));
      else if (seconds === 0) setText(holder, 'Chegou!');
      else if (style === 'big') setText(holder, `${parts.d} ${parts.d === 1 ? 'dia' : 'dias'}`);
      else setText(holder, `${parts.d}d ${pad(parts.h)}:${pad(parts.m)}:${pad(parts.s)}`);
    };
    paint();
    ctx.every(1000, paint);
  },
};

const pomodoro = {
  kind: 'pomodoro', title: 'Pomodoro', category: 'Tempo', size: { w: 280, h: 260 }, bare: true,
  styles: [['ring', 'Circular'], ['digital', 'Digital']],
  optionDefs: [{ key: 'work', label: 'Foco (min)', type: 'number', default: 25 }, { key: 'rest', label: 'Pausa (min)', type: 'number', default: 5 }],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const work = Math.max(1, Number(ctx.options.work) || 25) * 60;
    const rest = Math.max(1, Number(ctx.options.rest) || 5) * 60;
    let phase = 'work';
    let left = work;
    let endsAt = 0;
    const label = el('div', 'w-muted w-center');
    box.appendChild(label);
    const view = countdownDisplay(box, ctx.style === 'digital' ? 'digital' : 'ring');
    const paint = () => {
      if (endsAt) {
        left = Math.max(0, Math.round((endsAt - Date.now()) / 1000));
        if (left === 0) { phase = phase === 'work' ? 'rest' : 'work'; left = phase === 'work' ? work : rest; endsAt = Date.now() + left * 1000; }
      }
      setText(label, phase === 'work' ? 'Foco' : 'Pausa');
      view.set(left, phase === 'work' ? work : rest);
    };
    const buttons = controls(box, [
      ['go', 'Iniciar', () => { if (endsAt) { left = Math.round((endsAt - Date.now()) / 1000); endsAt = 0; } else endsAt = Date.now() + left * 1000; buttons.go.textContent = endsAt ? 'Pausar' : 'Iniciar'; paint(); }],
      ['reset', 'Zerar', () => { endsAt = 0; phase = 'work'; left = work; buttons.go.textContent = 'Iniciar'; paint(); }],
    ]);
    paint();
    ctx.every(500, () => { if (endsAt) paint(); });
  },
};

export const TIME_WIDGETS = [clockWidget, worldClock, bigDate, timer, countdown, pomodoro];
export { CLOCK_STYLES };
