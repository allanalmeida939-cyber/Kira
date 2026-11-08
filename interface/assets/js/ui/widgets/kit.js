/** Utilidades compartilhadas pelos widgets: criação de nós, formatação e dados do backend. */

export function el(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

export function setText(node, text) {
  const value = String(text ?? '');
  if (node.textContent !== value) node.textContent = value;
}

export function pad(value) {
  return String(value).padStart(2, '0');
}

/** Partes da hora em um fuso IANA (vazio = fuso local). Fuso inválido cai no local. */
export function timeParts(date = new Date(), tz = '') {
  let formatter;
  try {
    formatter = new Intl.DateTimeFormat('pt-BR', { timeZone: tz || undefined, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', weekday: 'long', day: '2-digit', month: 'long', year: 'numeric' });
  } catch (_) {
    formatter = new Intl.DateTimeFormat('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', weekday: 'long', day: '2-digit', month: 'long', year: 'numeric' });
  }
  const parts = Object.fromEntries(formatter.formatToParts(date).map((p) => [p.type, p.value]));
  return { h: Number(parts.hour) % 24, m: Number(parts.minute), s: Number(parts.second), weekday: parts.weekday, day: parts.day, month: parts.month, year: parts.year };
}

export function formatClock(parts, { hour12 = false, seconds = true } = {}) {
  const hour = hour12 ? (parts.h % 12) || 12 : parts.h;
  const suffix = hour12 ? (parts.h >= 12 ? ' PM' : ' AM') : '';
  return `${pad(hour)}:${pad(parts.m)}${seconds ? `:${pad(parts.s)}` : ''}${suffix}`;
}

export function formatDuration(totalSeconds) {
  const sign = totalSeconds < 0 ? '-' : '';
  const value = Math.abs(Math.floor(totalSeconds));
  const h = Math.floor(value / 3600);
  return `${sign}${h ? `${pad(h)}:` : ''}${pad(Math.floor((value % 3600) / 60))}:${pad(value % 60)}`;
}

export const WEATHER_TEXT = {
  0: 'Céu limpo', 1: 'Quase limpo', 2: 'Parcialmente nublado', 3: 'Nublado', 45: 'Neblina', 48: 'Neblina', 51: 'Garoa', 53: 'Garoa', 55: 'Garoa forte',
  61: 'Chuva fraca', 63: 'Chuva', 65: 'Chuva forte', 71: 'Neve fraca', 73: 'Neve', 75: 'Neve forte', 80: 'Pancadas', 81: 'Pancadas', 82: 'Pancadas fortes', 95: 'Trovoadas', 96: 'Trovoadas', 99: 'Trovoadas',
};

/** Mostra a indisponibilidade só dentro do widget. */
export function unavailable(box, message = 'indisponível') {
  clear(box);
  box.appendChild(el('p', 'w-muted w-center', message));
}

const SVG_NS = 'http://www.w3.org/2000/svg';

export function svg(tag, attrs = {}, parent = null) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
  if (parent) parent.appendChild(node);
  return node;
}

/** "agora", "há 5 min", "há 3 h", "há 2 d". */
export function relativeTime(epochSeconds, now = Date.now() / 1000) {
  const seconds = Math.max(0, now - Number(epochSeconds || now));
  if (seconds < 90) return 'agora';
  if (seconds < 3600) return `há ${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `há ${Math.round(seconds / 3600)} h`;
  return `há ${Math.round(seconds / 86400)} d`;
}

/** Variação com sinal tipográfico e vírgula decimal: −0,13% / +1,20%. */
export function formatPercent(value, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  const text = Math.abs(number).toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${number < 0 ? '−' : number > 0 ? '+' : ''}${text}%`;
}

export function formatBytesRate(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  if (number >= 1073741824) return `${(number / 1073741824).toFixed(1)} GB/s`;
  if (number >= 1048576) return `${(number / 1048576).toFixed(1)} MB/s`;
  return `${Math.round(number / 1024)} KB/s`;
}

export function formatBytes(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '--';
  if (number >= 1099511627776) return `${(number / 1099511627776).toFixed(1)} TB`;
  if (number >= 1073741824) return `${(number / 1073741824).toFixed(1)} GB`;
  return `${Math.round(number / 1048576)} MB`;
}

/** Aplica as opções comuns de aparência: cor de destaque e tamanho do texto (50–300%). */
export function applyAppearance(box, options = {}) {
  if (options.color) box.style.setProperty('--w-accent', String(options.color));
  const scale = Number(options.size);
  if (Number.isFinite(scale) && scale >= 50 && scale <= 300) box.style.fontSize = `${(13 * scale) / 100}px`;
}
