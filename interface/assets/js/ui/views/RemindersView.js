const SVG_NS = 'http://www.w3.org/2000/svg';
const ICONS = Object.freeze({
  bell: 'M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4',
  alarm: 'M12 21a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM12 9v4l2.5 1.5M5 3 2 6M19 3l3 3',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  repeat: 'M17 2l3 3-3 3M20 5H8a4 4 0 0 0-4 4M7 22l-3-3 3-3M4 19h12a4 4 0 0 0 4-4',
});
const STATUS_LABELS = Object.freeze({
  scheduled: 'Agendado',
  firing: 'Tocando',
  missed: 'Perdido',
  done: 'Concluído',
  dismissed: 'Dispensado',
});
const KINDS = Object.freeze([['reminder', 'Lembrete'], ['alarm', 'Alarme']]);
const REPEATS = Object.freeze([['none', 'Não'], ['daily', 'Todo dia'], ['weekly', 'Toda semana']]);
const SHORTCUTS = Object.freeze([[5, 'em 5 min'], [30, 'em 30 min'], [60, 'em 1 h']]);
const MAX_TEXT = 200;

function create(documentRef, tag, className = '', label = '') {
  const node = documentRef.createElement(tag);
  if (className) node.className = className;
  if (label) node.textContent = label;
  return node;
}

function icon(documentRef, name) {
  const holder = create(documentRef, 'span', 'kui-rem-icon');
  holder.setAttribute?.('aria-hidden', 'true');
  if (typeof documentRef.createElementNS !== 'function' || !ICONS[name]) return holder;
  const svg = documentRef.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  const path = documentRef.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', ICONS[name]);
  svg.appendChild(path);
  holder.appendChild(svg);
  return holder;
}

function localDateTime(offsetMinutes = 5) {
  const value = new Date(Date.now() + offsetMinutes * 60000);
  const pad = (part) => String(part).padStart(2, '0');
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}T${pad(value.getHours())}:${pad(value.getMinutes())}`;
}

function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || 'horário não informado');
  return new Intl.DateTimeFormat('pt-BR', { dateStyle: 'short', timeStyle: 'short' }).format(date);
}

function relativeDate(value) {
  const minutes = Math.round((new Date(value).getTime() - Date.now()) / 60000);
  if (Number.isNaN(minutes)) return '';
  if (minutes >= 0 && minutes < 60) return minutes === 0 ? 'agora' : `em ${minutes} min`;
  if (minutes < 0 && minutes > -60) return `há ${Math.abs(minutes)} min`;
  if (minutes >= 60 && minutes < 24 * 60) return `em ${Math.round(minutes / 60)} h`;
  return '';
}

export class RemindersView {
  constructor({ documentRef = globalThis.document, windowRef = globalThis.window, fetchRef = null } = {}) {
    this.document = documentRef;
    this.window = windowRef;
    this.fetchRef = fetchRef;
    this.container = null;
    this.context = null;
    this.list = null;
    this.status = null;
    this.counter = null;
    this.abort = null;
    this.timer = null;
    this.destroyed = false;
  }

  field(labelText, control) {
    const wrapper = create(this.document, 'div', 'kui-rem-field');
    wrapper.appendChild(create(this.document, 'span', 'kui-rem-label', labelText));
    wrapper.appendChild(control);
    return wrapper;
  }

  segmented(options, name) {
    const group = create(this.document, 'div', 'kui-rem-segmented');
    group.setAttribute('role', 'radiogroup');
    group.dataset.value = options[0][0];
    options.forEach(([value, title], index) => {
      const button = create(this.document, 'button', `kui-rem-seg${index === 0 ? ' is-active' : ''}`, title);
      button.type = 'button';
      button.setAttribute('role', 'radio');
      button.setAttribute('aria-checked', index === 0 ? 'true' : 'false');
      button.dataset.value = value;
      button.title = name;
      button.addEventListener('click', () => {
        group.dataset.value = value;
        Array.from(group.children).forEach((other) => {
          const active = other === button;
          other.className = `kui-rem-seg${active ? ' is-active' : ''}`;
          other.setAttribute('aria-checked', active ? 'true' : 'false');
        });
      });
      group.appendChild(button);
    });
    return group;
  }

  mount(container, context) {
    this.container = container;
    this.context = context;
    container.textContent = '';
    const form = create(this.document, 'form', 'kui-rem-form');

    const text = create(this.document, 'input', 'kui-rem-input kui-reminders-text');
    text.type = 'text'; text.maxLength = MAX_TEXT; text.required = true;
    text.placeholder = 'Ex.: beber água';
    text.setAttribute('aria-label', 'O que devo lembrar');
    this.counter = create(this.document, 'span', 'kui-rem-counter', `0/${MAX_TEXT}`);
    text.addEventListener('input', () => { this.counter.textContent = `${text.value.length}/${MAX_TEXT}`; });
    const textField = this.field('O que devo lembrar', text);
    textField.appendChild(this.counter);

    const due = create(this.document, 'input', 'kui-rem-input kui-reminders-due');
    due.type = 'datetime-local'; due.required = true; due.value = localDateTime();
    due.setAttribute('aria-label', 'Data e hora');
    const kind = this.segmented(KINDS, 'Tipo');
    const repeat = this.segmented(REPEATS, 'Repetir');

    const shortcuts = create(this.document, 'div', 'kui-rem-chips');
    SHORTCUTS.forEach(([minutes, title]) => {
      const chip = create(this.document, 'button', 'kui-rem-chip', title);
      chip.type = 'button';
      chip.addEventListener('click', () => { due.value = localDateTime(minutes); due.focus?.(); });
      shortcuts.appendChild(chip);
    });
    const dueField = this.field('Data e hora', due);
    dueField.appendChild(shortcuts);

    const grid = create(this.document, 'div', 'kui-rem-grid');
    grid.append(this.field('Tipo', kind), this.field('Repetir', repeat));

    const submit = create(this.document, 'button', 'kui-rem-submit', 'Agendar');
    submit.type = 'submit';
    this.status = create(this.document, 'p', 'kui-rem-status');
    this.status.setAttribute('role', 'status');
    form.append(textField, dueField, grid, submit, this.status);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      await this.create(text, due, kind, repeat, submit);
    });

    this.list = create(this.document, 'div', 'kui-rem-list');
    this.list.setAttribute('aria-live', 'polite');
    container.append(form, create(this.document, 'h3', 'kui-rem-title', 'Próximos avisos'), this.list);
    this.load();
    this.timer = (this.window?.setInterval || setInterval)(() => this.load(), 5000);
    return () => this.destroy();
  }

  async fetch(url, options = {}) {
    if (this.window && typeof this.window.fetch === 'function') return this.window.fetch(url, options);
    if (typeof this.fetchRef === 'function') return this.fetchRef(url, options);
    throw new Error('Transporte HTTP indisponível.');
  }

  isVisible() {
    if (this.destroyed || this.window?.document?.hidden) return false;
    const state = this.container?.closest?.('.kui-window')?.dataset?.kuiWindowState;
    return state !== 'minimized' && state !== 'closed';
  }

  async load() {
    if (!this.isVisible()) return;
    this.abort?.abort?.();
    this.abort = typeof AbortController === 'function' ? new AbortController() : null;
    try {
      const response = await this.fetch('/api/reminders', { cache: 'no-store', signal: this.abort?.signal });
      const payload = await response.json();
      if (!response.ok || payload?.success === false) throw new Error(payload?.error || `HTTP ${response.status}`);
      this.render(payload.items || payload.data?.reminders || []);
    } catch (error) {
      if (error?.name !== 'AbortError') this.setStatus(`Não consegui carregar os lembretes: ${error?.message || error}`, 'error');
    }
  }

  async create(text, due, kind, repeat, submit) {
    if (!text.value.trim() || !due.value) return;
    submit.disabled = true;
    try {
      const response = await this.fetch('/api/reminders', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text.value.trim(), due_at: new Date(due.value).toISOString(), kind: kind.dataset.value, repeat: repeat.dataset.value }),
      });
      const payload = await response.json();
      if (!response.ok || payload?.success === false) throw new Error(payload?.error || `HTTP ${response.status}`);
      text.value = ''; this.counter.textContent = `0/${MAX_TEXT}`;
      this.setStatus('Agendado. A Kira vai te avisar.', 'ok');
      await this.load();
    } catch (error) { this.setStatus(`Não consegui agendar: ${error?.message || error}`, 'error'); }
    finally { submit.disabled = false; }
  }

  render(items) {
    if (!this.list) return;
    this.list.textContent = '';
    const visible = (Array.isArray(items) ? items : []).filter((item) => !['done', 'dismissed'].includes(item.status));
    if (!visible.length) {
      const empty = create(this.document, 'div', 'kui-rem-empty');
      empty.appendChild(icon(this.document, 'bell'));
      empty.appendChild(create(this.document, 'p', 'kui-rem-empty-text', 'Nenhum aviso agendado.'));
      empty.appendChild(create(this.document, 'p', 'kui-rem-empty-hint', 'Peça por voz ("me lembra daqui a 10 minutos") ou agende acima.'));
      this.list.appendChild(empty);
      return;
    }
    visible.forEach((item) => {
      const status = item.status || 'scheduled';
      const when = item.snooze_until || item.due_at;
      const card = create(this.document, 'article', `kui-rem-card is-${status}`);
      card.appendChild(icon(this.document, item.kind === 'alarm' ? 'alarm' : 'bell'));
      const body = create(this.document, 'div', 'kui-rem-body');
      body.appendChild(create(this.document, 'strong', 'kui-rem-text', item.text || 'Lembrete sem texto'));
      const meta = create(this.document, 'div', 'kui-rem-meta');
      meta.appendChild(create(this.document, 'span', 'kui-rem-when', formatDate(when)));
      const relative = relativeDate(when);
      if (relative) meta.appendChild(create(this.document, 'span', 'kui-rem-relative', relative));
      if (item.fired_at) meta.appendChild(create(this.document, 'span', 'kui-rem-arrived', `Chegou ${formatDate(item.fired_at)}`));
      meta.appendChild(create(this.document, 'span', `kui-rem-badge is-${status}`, STATUS_LABELS[status] || status));
      if (item.repeat && item.repeat !== 'none') meta.appendChild(create(this.document, 'span', 'kui-rem-repeat', item.repeat === 'daily' ? 'Todo dia' : 'Toda semana'));
      body.appendChild(meta);
      card.appendChild(body);
      const actions = create(this.document, 'div', 'kui-rem-actions');
      if (status === 'firing') {
        actions.appendChild(this.action('Dispensar', 'kui-rem-btn', () => this.post(`/api/reminders/${encodeURIComponent(item.id)}/dismiss`)));
        // só alarme tem soneca; lembrete sai com Dispensar ou Excluir
        if (item.kind === 'alarm') actions.appendChild(this.action('Soneca 5 min', 'kui-rem-btn', () => this.post(`/api/reminders/${encodeURIComponent(item.id)}/snooze`, { minutes: 5 })));
        else actions.appendChild(this.action('Excluir', 'kui-rem-btn', () => this.remove(item.id)));
      } else {
        const remove = this.action('', 'kui-rem-btn kui-rem-delete', () => this.remove(item.id));
        remove.title = 'Excluir aviso';
        remove.setAttribute('aria-label', 'Excluir aviso');
        remove.appendChild(icon(this.document, 'trash'));
        actions.appendChild(remove);
      }
      card.appendChild(actions);
      this.list.appendChild(card);
    });
  }

  action(label, className, callback) {
    const button = create(this.document, 'button', className, label);
    button.type = 'button';
    button.addEventListener('click', callback);
    return button;
  }

  async post(url, body = {}) {
    try {
      const response = await this.fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await this.load();
    } catch (error) { this.setStatus(`Falha ao atualizar: ${error?.message || error}`, 'error'); }
  }

  async remove(id) {
    try {
      const response = await this.fetch(`/api/reminders/${encodeURIComponent(id)}`, { method: 'DELETE' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await this.load();
    } catch (error) { this.setStatus(`Falha ao excluir: ${error?.message || error}`, 'error'); }
  }

  setStatus(message, kind = 'info') {
    if (!this.status) return;
    this.status.textContent = String(message || '');
    this.status.dataset.kind = kind;
  }

  destroy() {
    this.destroyed = true;
    this.abort?.abort?.();
    if (this.timer != null) (this.window?.clearInterval || clearInterval)(this.timer);
    this.timer = null;
  }
}
