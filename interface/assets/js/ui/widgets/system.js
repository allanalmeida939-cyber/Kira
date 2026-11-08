import { el, clear, setText, svg, formatBytesRate, formatBytes, applyAppearance } from './kit.js';

const POINTS = 60;
const RING_LENGTH = 2 * Math.PI * 40;

const percent = (value) => (Number.isFinite(value) ? `${Math.round(value)}%` : 'indisponível');
const tone = (value) => (value >= 85 ? 'is-hot' : value >= 70 ? 'is-warm' : '');
const push = (list, value) => { list.push(Number.isFinite(value) ? value : 0); if (list.length > POINTS) list.shift(); };

function bar() {
  const track = el('div', 'w-bar');
  const fill = el('span');
  track.appendChild(fill);
  return { track, set(value) { fill.style.transform = `scaleX(${(Math.min(Math.max(value || 0, 0), 100) / 100).toFixed(3)})`; track.dataset.tone = tone(value); } };
}

function ring(label) {
  const root = svg('svg', { viewBox: '-50 -50 100 100', class: 'w-gauge' });
  svg('circle', { r: 40, class: 'w-ring-track' }, root);
  const arc = svg('circle', { r: 40, class: 'w-ring-arc', 'stroke-dasharray': RING_LENGTH.toFixed(2), 'stroke-dashoffset': RING_LENGTH.toFixed(2), transform: 'rotate(-90)' }, root);
  const value = svg('text', { x: 0, y: 4, 'text-anchor': 'middle', class: 'w-ring-text' }, root);
  const name = svg('text', { x: 0, y: 22, 'text-anchor': 'middle', class: 'w-ring-label' }, root);
  name.textContent = label;
  return {
    root,
    set(v) {
      const ok = Number.isFinite(v);
      arc.setAttribute('stroke-dashoffset', String(RING_LENGTH * (1 - (ok ? Math.min(Math.max(v, 0), 100) / 100 : 0))));
      value.textContent = ok ? `${Math.round(v)}%` : '—';
      root.dataset.tone = ok ? tone(v) : '';
    },
  };
}

function lineChart(max = 100) {
  const root = svg('svg', { viewBox: `0 0 ${POINTS} 30`, preserveAspectRatio: 'none', class: 'w-spark' });
  const area = svg('polygon', { class: 'w-spark-area' }, root);
  const line = svg('polyline', {}, root);
  return {
    root,
    set(values, ceiling = max) {
      const top = Math.max(ceiling, 1);
      const points = values.map((v, i) => `${POINTS - values.length + i},${(29 - (Math.min(v, top) / top) * 27).toFixed(1)}`);
      line.setAttribute('points', points.join(' '));
      area.setAttribute('points', points.length ? `${POINTS - values.length},30 ${points.join(' ')} ${POINTS - 1},30` : '');
    },
  };
}

function row(label) {
  const wrapper = el('div', 'w-row');
  const value = el('span', 'w-num', '--');
  wrapper.append(el('span', '', label), value);
  return { wrapper, value };
}

/** Widget de sistema: monta a interface e recebe quadros do coletor (só enquanto visível). */
function systemWidget(definition) {
  return {
    category: 'Sistema', bare: false, ...definition,
    mount(box, ctx) {
      applyAppearance(box, ctx.options);
      box.classList.add('w-system', `w-${definition.kind}`, `w-sys-${ctx.style || 'default'}`);
      const view = definition.build(box, ctx);
      return ctx.stats.subscribe((frame) => view.update(frame), { top: Boolean(definition.top) });
    },
  };
}

const vitals = systemWidget({
  kind: 'vitals', title: 'Sistema Vital', size: { w: 340, h: 300 },
  styles: [['bars', 'Barras'], ['gauges', 'Anéis'], ['lines', 'Gráficos de linha'], ['hud', 'HUD'], ['compact', 'Compacto (uma linha)']],
  build(box, ctx) {
    const style = ctx.style || 'bars';
    const hist = { cpu: [], ram: [] };
    const items = [['cpu', 'CPU'], ['ram', 'RAM'], ['disk', 'DISCO'], ['gpu', 'GPU']];
    const parts = {};
    if (style === 'compact') {
      const line = el('div', 'w-compact-line');
      box.appendChild(line);
      return { update(f) { setText(line, `CPU ${percent(f.cpu_percent)} · RAM ${percent(f.ram_percent)} · DISCO ${percent(f.disk?.percent)} · ↓ ${formatBytesRate(f.net?.down_bps)} ↑ ${formatBytesRate(f.net?.up_bps)}`); } };
    }
    if (style === 'gauges') {
      const grid = el('div', 'w-gauges');
      items.forEach(([key, label]) => { parts[key] = ring(label); grid.appendChild(parts[key].root); });
      box.appendChild(grid);
    } else {
      items.forEach(([key, label]) => {
        const r = row(label);
        const meter = style === 'lines' ? lineChart() : bar();
        box.append(r.wrapper, style === 'lines' ? meter.root : meter.track);
        parts[key] = { r, meter };
        if (style === 'hud') r.wrapper.classList.add('is-hud');
      });
    }
    const net = row('REDE ↓ / ↑');
    if (style !== 'gauges') box.appendChild(net.wrapper);
    const net2 = style === 'gauges' ? el('div', 'w-muted w-center') : null;
    if (net2) box.appendChild(net2);
    return {
      update(f) {
        const values = { cpu: f.cpu_percent, ram: f.ram_percent, disk: f.disk?.percent, gpu: f.gpu?.percent };
        push(hist.cpu, f.cpu_percent); push(hist.ram, f.ram_percent);
        items.forEach(([key]) => {
          const v = values[key];
          if (style === 'gauges') { parts[key].set(v); return; }
          setText(parts[key].r.value, percent(v));
          if (style === 'lines') parts[key].meter.set(key === 'cpu' ? hist.cpu : key === 'ram' ? hist.ram : [v || 0]);
          else parts[key].meter.set(v);
        });
        const text = `↓ ${formatBytesRate(f.net?.down_bps)} · ↑ ${formatBytesRate(f.net?.up_bps)}`;
        setText(net.value, text);
        if (net2) setText(net2, text);
      },
    };
  },
});

const cpuCores = systemWidget({
  kind: 'cpucores', title: 'CPU por núcleo', size: { w: 340, h: 240 },
  styles: [['grid', 'Grade de núcleos'], ['bars', 'Barras']],
  build(box, ctx) {
    const total = row('CPU total');
    const holder = el('div', ctx.style === 'bars' ? 'w-list' : 'w-core-grid');
    box.append(total.wrapper, holder);
    let cells = [];
    return {
      update(f) {
        setText(total.value, percent(f.cpu_percent));
        const cores = f.cores || [];
        if (cells.length !== cores.length) {
          clear(holder);
          cells = cores.map((_, i) => {
            const cell = el('div', ctx.style === 'bars' ? 'w-core-bar' : 'w-core');
            const meter = bar();
            const label = el('span', 'w-muted', `${i + 1}`);
            cell.append(label, meter.track);
            holder.appendChild(cell);
            return meter;
          });
        }
        cores.forEach((v, i) => cells[i].set(v));
        if (!cores.length) setText(total.value, 'indisponível');
      },
    };
  },
});

const ramWidget = systemWidget({
  kind: 'ram', title: 'Memória RAM', size: { w: 300, h: 220 },
  styles: [['detail', 'Detalhada'], ['ring', 'Anel'], ['bar', 'Barra']],
  build(box, ctx) {
    const style = ctx.style || 'detail';
    if (style === 'ring') {
      const gauge = ring('RAM');
      const text = el('div', 'w-muted w-center');
      box.append(gauge.root, text);
      return { update(f) { gauge.set(f.ram_percent); setText(text, f.memory ? `${formatBytes(f.memory.used)} de ${formatBytes(f.memory.total)}` : ''); } };
    }
    const head = row('Em uso');
    const meter = bar();
    box.append(head.wrapper, meter.track);
    const lines = style === 'detail' ? { free: row('Disponível'), total: row('Total'), swap: row('Arquivo de paginação') } : {};
    Object.values(lines).forEach((l) => box.appendChild(l.wrapper));
    return {
      update(f) {
        const m = f.memory;
        setText(head.value, m ? `${formatBytes(m.used)} (${percent(m.percent)})` : percent(f.ram_percent));
        meter.set(f.ram_percent);
        if (m && lines.free) { setText(lines.free.value, formatBytes(m.available)); setText(lines.total.value, formatBytes(m.total)); setText(lines.swap.value, `${formatBytes(m.swap_used)} / ${formatBytes(m.swap_total)}`); }
      },
    };
  },
});

const diskWidget = systemWidget({
  kind: 'disk', title: 'Disco', size: { w: 320, h: 240 },
  styles: [['units', 'Por unidade'], ['activity', 'Atividade']],
  build(box, ctx) {
    const list = el('div', 'w-list');
    const read = row('Leitura');
    const write = row('Escrita');
    box.append(list, read.wrapper, write.wrapper);
    const unitRows = new Map();
    const chart = lineChart();
    const reads = [];
    if (ctx.style === 'activity') { list.hidden = true; box.appendChild(chart.root); }
    return {
      update(f) {
        setText(read.value, formatBytesRate(f.disk?.read_bps));
        setText(write.value, formatBytesRate(f.disk?.write_bps));
        push(reads, (f.disk?.read_bps || 0) + (f.disk?.write_bps || 0));
        chart.set(reads, Math.max(...reads, 1));
        (f.disks || []).forEach((d) => {
          let unit = unitRows.get(d.mount);
          if (!unit) { unit = { r: row(d.mount), meter: bar() }; unitRows.set(d.mount, unit); list.append(unit.r.wrapper, unit.meter.track); }
          setText(unit.r.value, `${formatBytes(d.used)} / ${formatBytes(d.total)} · ${percent(d.percent)}`);
          unit.meter.set(d.percent);
        });
      },
    };
  },
});

const networkWidget = systemWidget({
  kind: 'network', title: 'Rede', size: { w: 320, h: 230 },
  styles: [['graph', 'Gráfico'], ['numbers', 'Números grandes']],
  build(box, ctx) {
    const down = row('Download');
    const up = row('Upload');
    box.append(down.wrapper, up.wrapper);
    const downs = [];
    const ups = [];
    const chartDown = lineChart();
    const chartUp = lineChart();
    if (ctx.style !== 'numbers') box.append(chartDown.root, chartUp.root);
    else { down.value.classList.add('w-big'); up.value.classList.add('w-big'); }
    return {
      update(f) {
        push(downs, f.net?.down_bps); push(ups, f.net?.up_bps);
        setText(down.value, `↓ ${formatBytesRate(f.net?.down_bps)}`);
        setText(up.value, `↑ ${formatBytesRate(f.net?.up_bps)}`);
        const peak = Math.max(...downs, ...ups, 1);
        chartDown.set(downs, peak); chartUp.set(ups, peak);
      },
    };
  },
});

const gpuWidget = systemWidget({
  kind: 'gpu', title: 'GPU', size: { w: 260, h: 200 },
  styles: [['ring', 'Anel'], ['bar', 'Barra']],
  build(box, ctx) {
    if (ctx.style === 'bar') {
      const head = row('GPU');
      const meter = bar();
      box.append(head.wrapper, meter.track);
      return { update(f) { setText(head.value, percent(f.gpu?.percent)); meter.set(f.gpu?.percent); } };
    }
    const gauge = ring('GPU');
    const note = el('div', 'w-muted w-center');
    box.append(gauge.root, note);
    return { update(f) { gauge.set(f.gpu?.percent); setText(note, Number.isFinite(f.gpu?.percent) ? '' : 'indisponível nesta máquina'); } };
  },
});

const batteryWidget = systemWidget({
  kind: 'battery', title: 'Temperatura e bateria', size: { w: 280, h: 190 },
  build(box) {
    const battery = row('Bateria');
    const temp = row('Temperatura');
    const meter = bar();
    box.append(battery.wrapper, meter.track, temp.wrapper);
    return {
      update(f) {
        setText(battery.value, f.battery ? `${Math.round(f.battery.percent)}% ${f.battery.plugged ? '· carregando' : ''}` : 'indisponível');
        meter.set(f.battery?.percent);
        setText(temp.value, Number.isFinite(f.temperature) ? `${Math.round(f.temperature)}°C` : 'indisponível');
      },
    };
  },
});

const uptimeWidget = systemWidget({
  kind: 'uptime', title: 'Tempo ligado', size: { w: 260, h: 140 }, bare: true,
  styles: [['digital', 'Digital'], ['text', 'Texto']],
  build(box, ctx) {
    const value = el('div', 'w-time');
    box.append(el('div', 'w-muted w-center', 'Ligado há'), value);
    return {
      update(f) {
        if (!f.boot_time) { setText(value, 'indisponível'); return; }
        const seconds = Math.max(0, Date.now() / 1000 - f.boot_time);
        const d = Math.floor(seconds / 86400); const h = Math.floor((seconds % 86400) / 3600); const m = Math.floor((seconds % 3600) / 60);
        setText(value, ctx.style === 'text' ? `${d ? `${d} d ` : ''}${h} h ${m} min` : `${d ? `${d}d ` : ''}${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`);
      },
    };
  },
});

const topApps = systemWidget({
  kind: 'topapps', title: 'Top aplicativos', size: { w: 340, h: 320 }, top: true,
  styles: [['cpu', 'Barras de CPU'], ['list', 'Lista']],
  build(box, ctx) {
    const list = el('div', 'w-list');
    box.appendChild(list);
    return {
      update(f) {
        clear(list);
        if (!f.top) { list.appendChild(el('p', 'w-muted w-center', 'carregando…')); return; }
        f.top.forEach((app) => {
          const r = row(app.name);
          setText(r.value, `${app.cpu.toFixed(1)}% · ${formatBytes(app.mem)}`);
          list.appendChild(r.wrapper);
          if (ctx.style !== 'list') { const meter = bar(); meter.set(Math.min(app.cpu * 4, 100)); list.appendChild(meter.track); }
        });
      },
    };
  },
});

const openApps = systemWidget({
  kind: 'openapps', title: 'Aplicativos abertos', size: { w: 300, h: 280 }, top: true,
  styles: [['chips', 'Etiquetas'], ['list', 'Lista']],
  build(box, ctx) {
    const holder = el('div', ctx.style === 'list' ? 'w-list' : 'w-chips');
    box.appendChild(holder);
    return {
      update(f) {
        clear(holder);
        (f.apps || []).forEach((name) => holder.appendChild(el(ctx.style === 'list' ? 'div' : 'span', ctx.style === 'list' ? 'w-row' : 'w-chip', name)));
        if (!holder.firstChild) holder.appendChild(el('p', 'w-muted w-center', f.apps ? 'nenhum aplicativo com janela' : 'carregando…'));
      },
    };
  },
});

export const SYSTEM_WIDGETS = [vitals, cpuCores, ramWidget, diskWidget, networkWidget, gpuWidget, batteryWidget, uptimeWidget, topApps, openApps];
