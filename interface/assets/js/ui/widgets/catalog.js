import { TIME_WIDGETS } from './time.js';
import { CALENDAR_WIDGETS } from './calendar.js';
import { INFO_WIDGETS } from './info.js';
import { NEWS_WIDGETS } from './news.js';
import { GAMES_WIDGETS } from './games.js';
import { SYSTEM_WIDGETS } from './system.js';
import { KIRA_WIDGETS } from './kira.js';
import { PRODUCTIVITY_WIDGETS } from './productivity.js';

export const CATEGORIES = ['Tempo', 'Informação', 'Entretenimento', 'Sistema', 'Kira', 'Produtividade'];

const ALL = [...TIME_WIDGETS, ...CALENDAR_WIDGETS, ...INFO_WIDGETS, ...NEWS_WIDGETS, ...GAMES_WIDGETS, ...SYSTEM_WIDGETS, ...KIRA_WIDGETS, ...PRODUCTIVITY_WIDGETS];
const BY_KIND = new Map(ALL.map((widget) => [widget.kind, widget]));

export const catalog = () => ALL.slice();
export const widgetFor = (kind) => BY_KIND.get(kind) || null;

const plain = (value) => String(value || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();

/** Texto pesquisável da galeria: título, categoria, tipo e nomes dos estilos. */
export function matches(widget, query, category) {
  const styles = (widget.styles || []).map(([, label]) => label).join(' ');
  return (!category || widget.category === category) && plain(`${widget.title} ${widget.category} ${widget.kind} ${styles}`).includes(plain(query));
}

/** Número de variações de um widget (estilos), para mostrar na galeria. */
export const variantCount = (widget) => (widget.styles ? widget.styles.length : 1);
