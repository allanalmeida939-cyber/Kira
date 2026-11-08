import AlarmSound from './shell/alarmSound.js';

const POLL_MS = 2000;
const MAX_INBOX_ITEMS = 50;

function make(documentRef, tag, className = '', text = '') {
  const node = documentRef.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = String(text);
  return node;
}

function safeText(value, fallback = '') {
  const text = String(value ?? '').trim();
  return text || fallback;
}

const hhmm = (value) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
};

// Todo aviso diz a hora que chegou; alarme diz também a hora marcada.
export function timeLine(item = {}) {
  const arrived = hhmm(item.fired_at || Date.now());
  const due = item.kind === 'alarm' ? hhmm(item.due_at) : '';
  return [arrived && `Chegou às ${arrived}`, due && `alarme marcado para ${due}`].filter(Boolean).join(' · ');
}

export class ReminderNotifications {
  constructor({ documentRef = globalThis.document, windowRef = globalThis.window, fetchRef = null, logger = globalThis.console } = {}) {
    this.document = documentRef;
    this.window = windowRef;
    this.fetchRef = fetchRef;
    this.logger = logger;
    this.sequence = 0;
    this.inbox = [];
    this.unread = 0;
    this.timer = null;
    this.destroyed = false;
    this.sound = new AlarmSound({ windowRef, logger });
    this.seen = new Set();
  }

  mount() {
    if (!this.document?.body) return this;
    this.list = this.document.getElementById('reminder-inbox-list');
    this.count = this.document.getElementById('reminder-count');
    this.bell = this.document.getElementById('drawer-reminders-btn-float');
    this.clearButton = this.document.getElementById('reminder-inbox-clear');
    this.bell?.addEventListener('click', () => this.markRead());
    this.clearButton?.addEventListener('click', () => {
      this.inbox = [];
      this.renderInbox();
    });
    this.loadBaseline().finally(() => this.poll());
    this.timer = (this.window?.setInterval || setInterval)(() => this.poll(), POLL_MS);
    return this;
  }

  async fetch(url, options = {}) {
    if (this.window && typeof this.window.fetch === 'function') return this.window.fetch(url, options);
    if (typeof this.fetchRef === 'function') return this.fetchRef(url, options);
    throw new Error('Transporte HTTP indisponível.');
  }

  async json(url, options = {}) {
    const response = await this.fetch(url, options);
    const payload = await response.json();
    if (!response.ok || payload?.success === false) throw new Error(payload?.error || `HTTP ${response.status}`);
    return payload;
  }

  async loadBaseline() {
    try {
      const events = await this.json('/api/reminders/events?since=0', { cache: 'no-store' });
      this.sequence = Number(events.seq || 0);
      const current = await this.json('/api/reminders', { cache: 'no-store' });
      for (const item of (current.items || current.data?.reminders || [])) {
        if (item?.status === 'firing' || item?.status === 'missed') {
          this.handleEvent({ type: item.status === 'missed' ? 'reminder_missed' : 'reminder_fired', reminder: item }, true);
        }
      }
      const pending = await this.json('/api/reminders/voice-pending', { cache: 'no-store' });
      for (const item of (pending.items || [])) {
        this.handleEvent({ type: item.status === 'missed' ? 'reminder_missed' : 'reminder_fired', reminder: item }, true);
      }
    } catch (error) {
      this.logger?.debug?.('[Kira] caixa de lembretes aguardando backend', error);
    }
  }

  async poll() {
    if (this.destroyed) return;
    try {
      const payload = await this.json(`/api/reminders/events?since=${encodeURIComponent(this.sequence)}`, { cache: 'no-store' });
      this.sequence = Math.max(this.sequence, Number(payload.seq || 0));
      for (const event of (payload.events || [])) this.handleEvent(event, false);
    } catch (error) {
      this.logger?.debug?.('[Kira] polling de lembretes indisponível', error);
    }
  }

  handleEvent(event, fromStartup = false) {
    const item = event?.reminder;
    const id = safeText(item?.id);
    if (!id || this.seen.has(`${event?.type}:${id}:${item?.fired_at || ''}`)) return;
    this.seen.add(`${event?.type}:${id}:${item?.fired_at || ''}`);
    this.inbox.unshift({ event, item });
    this.inbox = this.inbox.slice(0, MAX_INBOX_ITEMS);
    this.unread += 1;
    this.renderInbox();
    const title = event.type === 'reminder_missed' ? 'Aviso perdido' : item.kind === 'alarm' ? 'Alarme da Kira' : 'Lembrete da Kira';
    const message = safeText(item.text, 'Você tem um aviso agendado.');
    this.showToast(title, message, item);
    if (item.kind === 'alarm') this.sound.startAlarm();
    else if (!fromStartup || item.status === 'missed') this.sound.playReminder();
  }

  renderInbox() {
    if (!this.list) return;
    this.list.textContent = '';
    if (!this.inbox.length) {
      this.list.appendChild(make(this.document, 'div', 'reminder-inbox-empty', 'Nenhum aviso por enquanto.'));
    } else {
      for (const entry of this.inbox) {
        const row = make(this.document, 'article', 'reminder-inbox-entry');
        row.appendChild(make(this.document, 'strong', '', safeText(entry.item.kind === 'alarm' ? 'ALARME' : 'LEMBRETE')));
        row.appendChild(make(this.document, 'p', '', safeText(entry.item.text, 'Aviso sem texto.')));
        row.appendChild(make(this.document, 'small', 'reminder-inbox-time', timeLine(entry.item)));
        row.appendChild(this.actions('reminder-inbox-actions', entry.item));
        this.list.appendChild(row);
      }
    }
    if (this.count) {
      this.count.textContent = String(this.unread);
      this.count.hidden = this.unread <= 0;
    }
  }

  showToast(title, message, item) {
    let host = this.document.getElementById('kira-reminder-toasts');
    if (!host) {
      host = make(this.document, 'div', 'kira-reminder-toasts');
      host.id = 'kira-reminder-toasts';
      this.document.body.appendChild(host);
    }
    const toast = make(this.document, 'article', 'kira-reminder-toast');
    toast.appendChild(make(this.document, 'strong', '', title));
    toast.appendChild(make(this.document, 'p', '', message));
    toast.appendChild(make(this.document, 'small', 'kira-reminder-toast-time', timeLine(item)));
    toast.appendChild(this.actions('kira-reminder-toast-actions', item, () => toast.remove()));
    host.appendChild(toast);
    const timeout = this.window?.setTimeout || setTimeout;
    timeout(() => toast.remove(), 15_000);
  }

  async post(id, action, body = {}) {
    try {
      await this.json(`/api/reminders/${encodeURIComponent(id)}/${action}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      this.sound.stop();
      this.inbox = this.inbox.filter((entry) => entry.item.id !== id);
      this.renderInbox();
    } catch (error) {
      this.logger?.warn?.('[Kira] ação do lembrete falhou', error);
    }
  }

  // Só alarme tem soneca; lembrete: Dispensar ou Excluir.
  actions(className, item, done = () => {}) {
    const box = make(this.document, 'div', className);
    const button = (label, run) => {
      const b = make(this.document, 'button', '', label);
      b.type = 'button';
      b.addEventListener('click', () => { run(item.id); done(); });
      box.appendChild(b);
    };
    button('Dispensar', (id) => this.dismiss(id));
    if (item.kind === 'alarm') button('Soneca 5 min', (id) => this.snooze(id));
    else button('Excluir', (id) => this.remove(id));
    return box;
  }

  async remove(id) {
    try { await this.json(`/api/reminders/${encodeURIComponent(id)}`, { method: 'DELETE' }); } catch (error) { this.logger?.warn?.('[Kira] excluir aviso falhou', error); }
    this.sound.stop();
    this.inbox = this.inbox.filter((entry) => entry.item.id !== id);
    this.renderInbox();
  }

  dismiss(id) { return this.post(id, 'dismiss'); }
  snooze(id) { return this.post(id, 'snooze', { minutes: 5 }); }
  markRead() { this.unread = 0; this.renderInbox(); }

  destroy() {
    this.destroyed = true;
    if (this.timer != null) (this.window?.clearInterval || clearInterval)(this.timer);
    this.sound.stop();
  }
}

export function mountReminderNotifications(options = {}) {
  return new ReminderNotifications(options).mount();
}

function enabledByFlag() {
  try {
    const query = new URLSearchParams(globalThis.location?.search || '');
    if (query.get('kiraui') === '0') return false;
    if (query.get('kiraui') === '1') return true;
    return globalThis.localStorage?.getItem('kira.ui.enabled') === '1';
  } catch (_) {
    return false;
  }
}

if (typeof document !== 'undefined' && enabledByFlag()) {
  const start = () => { window.KiraReminderNotifications = mountReminderNotifications(); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
}
