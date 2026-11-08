/** @typedef {'idle'|'listening'|'thinking'|'searching'|'speaking'} KiraState */
/** @typedef {'transcript'|'apis'|'gallery'|'video'|'article'|'search-log'|'scoreboard'|'viewer'|'election'|'info'|'download'|'creation'|'notifications'|'whatsapp'|'music'|'memory'|'logs'|'tutor'|'reminders'|'smart_home'} PanelKind */
/** @typedef {'main'|'chat'} ViewId */

/**
 * @typedef {(
 *  {type:'status', state:KiraState, scope?:'media', text?:string, steps?:{label:string,done:boolean}[]} |
 *  {type:'open_panel', kind:PanelKind, id?:string, title?:string, props?:object, focus?:boolean, autoCloseMs?:number, view?:ViewId} |
 *  {type:'update_panel', id:string, props:object} |
 *  {type:'close_panel', id:string, preserveUserTouched?:boolean} |
 *  {type:'media_cue', panelId:string, index:number} |
 *  {type:'layout', preset:string}
 * )} KiraAction
 */

export const KIRA_STATES = Object.freeze(['idle', 'listening', 'thinking', 'searching', 'speaking']);
export const PANEL_KINDS = Object.freeze([
  'transcript', 'apis', 'gallery', 'video', 'article', 'search-log', 'scoreboard', 'viewer', 'election', 'info', 'download', 'creation',
  'notifications', 'whatsapp', 'music', 'memory', 'logs', 'tutor', 'reminders', 'smart_home', 'quadro',
]);
export const VIEW_IDS = Object.freeze(['main', 'chat']);
export const ACTION_TYPES = Object.freeze([
  'status', 'open_panel', 'update_panel', 'close_panel', 'media_cue', 'layout', 'present', 'present_patch', 'screen_locate_result', 'ui_control', 'ui_call',
]);
export const SCREEN_LOCATE_STATES = Object.freeze(['looking', 'found', 'not_found', 'ambiguous', 'error', 'cleared']);
export const PRESENTATION_STATES = Object.freeze(['preparing_query', 'researching', 'collecting_media', 'organizing', 'ready', 'partial', 'error', 'cancelled']);
export const SECTION_TYPES = Object.freeze(['text', 'image_gallery', 'timeline', 'video', 'chart', 'sources', 'weather', 'agenda', 'email', 'news', 'election', 'briefing', 'health', 'recipe', 'instagram']);
const SECTION_STATES = Object.freeze(['pending', 'ready', 'unavailable']);
const DATA_KEY_RE = /^[A-Za-z][A-Za-z0-9_]{0,40}$/;
const SECTION_URL_KEY_RE = /(url|href|link|image|thumbnail|poster|src)$/i;

const ID_RE = /^[a-z0-9_-]{1,64}$/i;
const URL_KEY_RE = /(url|href|src|link|image|thumbnail|poster)/i;
const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;
const INVALID = Symbol('invalid');
const SAFE_PROP_KEYS = new Set([
  'items', 'query', 'content', 'body', 'description', 'text', 'url', 'imageUrl', 'image',
  'thumbnail', 'poster', 'link', 'title', 'source', 'sources', 'steps', 'logs', 'status',
  'value', 'index', 'duration', 'media', 'videos', 'articles', 'images', 'results', 'label',
  'done', 'kind', 'id', 'name', 'date', 'author', 'category', 'href', 'src',
  'license', 'licenses', 'videoId', 'types', 'time', 'timestamp', 'ok', 'level', 'append', 'alt', 'sourceUrl',
  'home', 'away', 'homeScore', 'awayScore', 'minute', 'league', 'state', 'team', 'venue', 'updatedAt', 'homeLogoUrl', 'awayLogoUrl', 'stale', 'caption', 'excerpt', 'party', 'number', 'votes', 'pct', 'photoUrl', 'apurado', 'elected', 'summary', 'facts',
  'beats', 'activeBeat', 'keywords',
]);

function byteLength(value) {
  try { return new TextEncoder().encode(value).length; } catch (_) { return unescape(encodeURIComponent(value)).length; }
}

function isPlainObject(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function boundedString(value, maxLength = 2000) {
  if (typeof value !== 'string') return null;
  const result = value.trim();
  if (result.length > maxLength) return null;
  return result;
}

function validId(value, required = true) {
  if (value == null && !required) return undefined;
  return typeof value === 'string' && ID_RE.test(value) ? value : null;
}

const IMAGE_KEY_RE = /(image|imageurl|thumbnail|poster|src|logourl|photourl)$/i;
const PROXY_PATH = '/api/ui/img?u=';
// Imagens criadas pela Kira (services/image_creation.py), servidas pelo próprio backend.
const CREATION_PATH_RE = /^\/api\/ui\/creation\/file\/[a-z0-9-]{1,60}-\d{10}\.(?:jpg|png|webp)$/;
// Radar montado pelo backend (services/sky_watch.py).
const LOCAL_IMAGE_RE = /^\/api\/(sky\/image\/radar-[0-9a-f]{10}\.png|vision\/image\/cam-[0-9a-f]{10}\.jpg)$/;
const KNOWN_IMAGE_HOSTS = new Set(['upload.wikimedia.org', 'thumb.wikimedia.org', 'commons.wikimedia.org', 'i.ytimg.com', 'api.openverse.org']);

function isPrivateIpv4(host) {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (!match) return false;
  const [a, b] = [Number(match[1]), Number(match[2])];
  return a === 0 || a === 10 || a === 127 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127) || a >= 224;
}

function sanitizeUrl(value, key = '') {
  if (typeof value !== 'string') return null;
  const isImage = IMAGE_KEY_RE.test(key);
  if (isImage && value.startsWith(PROXY_PATH)) return value.length <= 2000 && !/\s/.test(value) && [...value].every((ch) => ch.charCodeAt(0) > 31) ? value : null;
  if (isImage && (CREATION_PATH_RE.test(value) || LOCAL_IMAGE_RE.test(value))) return value;
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password) return null;
    const host = url.hostname.toLowerCase();
    if (host === 'localhost' || !host.includes('.') || host.includes(':') || isPrivateIpv4(host)) return null;
    if (isImage && !KNOWN_IMAGE_HOSTS.has(host)) return null;
    return url.href;
  } catch (_) {
    return null;
  }
}

function sanitizeValue(value, key = '', depth = 0) {
  if (depth > 4) return undefined;
  if (typeof value === 'string') {
    if (URL_KEY_RE.test(key)) return sanitizeUrl(value, key) || INVALID;
    return boundedString(value);
  }
  if (typeof value === 'number') return Number.isFinite(value) ? value : undefined;
  if (typeof value === 'boolean' || value === null) return value;
  if (Array.isArray(value)) {
    const result = [];
    for (const item of value.slice(0, 24)) {
      const normalized = sanitizeValue(item, key, depth + 1);
      if (normalized === INVALID) return INVALID;
      if (normalized !== undefined) result.push(normalized);
    }
    return result;
  }
  if (!isPlainObject(value)) return undefined;
  const result = {};
  for (const [childKey, childValue] of Object.entries(value)) {
    if (!SAFE_PROP_KEYS.has(childKey) && !SAFE_PROP_KEYS.has(childKey.toLowerCase())) continue;
    const normalized = sanitizeValue(childValue, childKey, depth + 1);
    if (normalized === INVALID) return INVALID;
    if (normalized !== undefined) result[childKey] = normalized;
  }
  return result;
}

function sanitizeProps(value, kind) {
  if (value == null) return {};
  if (!isPlainObject(value)) return null;
  // Estudos: simulado/correção/painel têm campos próprios e mais de 4 níveis; o TutorPanel só usa textContent e a
  // imagem só de upload.wikimedia.org, então passa inteiro (o tamanho já é limitado em parseRaw a 256 KB).
  if (kind === 'tutor' || kind === 'quadro') { // quadro: mesmo caso (QuadroPanel só usa textContent e safeImage)
    try { return JSON.parse(JSON.stringify(value)); } catch (_) { return null; }
  }
  const props = sanitizeValue(value);
  if (props === INVALID || !props || !isPlainObject(props)) return null;
  if (Array.isArray(props.items)) {
    const limit = kind === 'video' ? 6 : kind === 'gallery' ? 24 : 24;
    if (props.items.length > limit) return null;
  }
  if (kind === 'video') {
    if (props.videoId != null && (typeof props.videoId !== 'string' || !VIDEO_ID_RE.test(props.videoId.trim()))) return null;
    if (Array.isArray(props.items) && props.items.some((item) => item?.videoId != null && (typeof item.videoId !== 'string' || !VIDEO_ID_RE.test(item.videoId.trim())))) return null;
  }
  return props;
}

function parseRaw(raw) {
  if (typeof raw === 'string') {
    if (byteLength(raw) > 256 * 1024) return null;
    try { return JSON.parse(raw); } catch (_) { return null; }
  }
  if (!isPlainObject(raw)) return null;
  try {
    const serialized = JSON.stringify(raw);
    if (byteLength(serialized) > 256 * 1024) return null;
  } catch (_) { return null; }
  return raw;
}

function normalizeSteps(value) {
  if (value == null) return undefined;
  if (!Array.isArray(value) || value.length > 24) return null;
  const steps = [];
  for (const step of value) {
    if (!isPlainObject(step)) return null;
    const label = boundedString(step.label, 120);
    if (label == null || typeof step.done !== 'boolean') return null;
    steps.push({ label, done: step.done });
  }
  return steps;
}

/**
 * Validate and normalize a Kira -> UI action. Unknown top-level fields are dropped.
 * @param {unknown} raw
 * @returns {KiraAction|null}
 */
export function parseAction(raw) {
  const source = parseRaw(raw);
  if (!source || typeof source.type !== 'string' || !ACTION_TYPES.includes(source.type)) return null;

  if (source.type === 'present' || source.type === 'present_patch') return parsePresentation(source);

  if (source.type === 'screen_locate_result') {
    const requestId = validId(source.requestId, true);
    if (!requestId || !SCREEN_LOCATE_STATES.includes(source.state)) return null;
    const action = { type: 'screen_locate_result', requestId, state: source.state };
    if (source.target != null) {
      const target = boundedString(source.target, 120);
      if (target == null) return null;
      action.target = target;
    }
    if (Number.isInteger(source.monitor) && source.monitor >= 1 && source.monitor <= 16) action.monitor = source.monitor;
    if (Array.isArray(source.matches)) {
      action.matches = source.matches.slice(0, 10).map((match) => ({
        label: boundedString(match?.label, 80) || '',
        confidence: Number.isFinite(match?.confidence) ? Math.min(1, Math.max(0, match.confidence)) : 0,
      }));
    }
    return action;
  }

  if (source.type === 'status') {
    if (!KIRA_STATES.includes(source.state)) return null;
    const action = { type: 'status', state: source.state };
    if (source.scope != null) {
      if (source.scope !== 'media') return null;
      action.scope = 'media';
    }
    if (source.text != null) {
      const text = boundedString(source.text);
      if (text == null) return null;
      action.text = text;
    }
    const steps = normalizeSteps(source.steps);
    if (steps === null) return null;
    if (steps !== undefined) action.steps = steps;
    return action;
  }

  if (source.type === 'open_panel') {
    if (!PANEL_KINDS.includes(source.kind)) return null;
    const action = { type: 'open_panel', kind: source.kind };
    const id = validId(source.id, false);
    if (id === null) return null;
    if (id !== undefined) action.id = id;
    if (source.title != null) {
      const title = boundedString(source.title, 120);
      if (title == null) return null;
      action.title = title;
    }
    const props = sanitizeProps(source.props, source.kind);
    if (props === null) return null;
    if (Object.keys(props).length) action.props = props;
    if (source.focus != null) {
      if (typeof source.focus !== 'boolean') return null;
      action.focus = source.focus;
    }
    if (source.autoCloseMs != null) {
      if (!Number.isInteger(source.autoCloseMs) || source.autoCloseMs < 0 || source.autoCloseMs > 600000) return null;
      action.autoCloseMs = source.autoCloseMs;
    }
    if (source.view != null) {
      if (!VIEW_IDS.includes(source.view)) return null;
      action.view = source.view;
    }
    return action;
  }

  if (source.type === 'update_panel') {
    const id = validId(source.id);
    const props = sanitizeProps(source.props);
    if (!id || props === null) return null;
    return { type: 'update_panel', id, props };
  }

  if (source.type === 'close_panel') {
    const id = validId(source.id);
    if (!id) return null;
    const action = { type: 'close_panel', id };
    if (source.preserveUserTouched != null) {
      if (typeof source.preserveUserTouched !== 'boolean') return null;
      action.preserveUserTouched = source.preserveUserTouched;
    }
    return action;
  }

  if (source.type === 'ui_call') {
    // ida e volta com a tela do celular (Olho de Deus): só alvos e métodos conhecidos
    const methods = { godseye: ['command', 'showPlace'], rota: ['show'] };
    if (typeof source.id !== 'string' || !/^[0-9a-f]{8,40}$/.test(source.id)) return null;
    if (!(methods[source.target] || []).includes(source.method)) return null;
    if (!source.args || typeof source.args !== 'object' || Array.isArray(source.args)) return null;
    return { type: 'ui_call', id: source.id, target: source.target, method: source.method, args: source.args };
  }

  if (source.type === 'ui_control') {
    // música, vídeo e abas no celular (pedido feito por lá abre lá); o backend já validou os campos da faixa
    const tabs = ['kira', 'whatsapp', 'config', 'browser', 'music', 'estudos', 'casa'];
    if (source.command === 'open_tab') return tabs.includes(source.value) ? { type: 'ui_control', command: 'open_tab', value: source.value } : null;
    const commands = ['play_item', 'play_pause', 'play', 'pause', 'next', 'previous', 'stop', 'set_volume', 'volume_up', 'volume_down',
      'fullscreen', 'exit_fullscreen', 'close', 'minimize', 'restore'];
    if (!commands.includes(source.command)) return null;
    const value = source.value;
    if (value != null && typeof value !== 'number' && (typeof value !== 'object' || Array.isArray(value))) return null;
    return { type: 'ui_control', command: source.command, value: value ?? null };
  }

  if (source.type === 'media_cue') {
    const panelId = validId(source.panelId);
    if (!panelId || !Number.isInteger(source.index) || source.index < 0 || source.index > 10000) return null;
    return { type: 'media_cue', panelId, index: source.index };
  }

  const presets = ['default', 'main', 'focus', 'grid', 'split', 'research'];
  return presets.includes(source.preset) ? { type: 'layout', preset: source.preset } : null;
}

/** Dados de seção: chaves simples, texto limitado, links https públicos, imagens só pelo proxy local. */
function sanitizeSectionData(value, key = '', depth = 0) {
  if (depth > 6) return INVALID;
  if (typeof value === 'string') {
    if (SECTION_URL_KEY_RE.test(key)) return sanitizeUrl(value, key) || INVALID;
    const text = boundedString(value, 4000);
    return text == null ? INVALID : text;
  }
  if (typeof value === 'number') return Number.isFinite(value) ? value : INVALID;
  if (typeof value === 'boolean' || value === null) return value;
  if (Array.isArray(value)) {
    if (value.length > 40) return INVALID;
    const items = value.map((item) => sanitizeSectionData(item, key, depth + 1));
    return items.includes(INVALID) ? INVALID : items;
  }
  if (!isPlainObject(value)) return INVALID;
  const result = {};
  for (const [childKey, child] of Object.entries(value)) {
    if (!DATA_KEY_RE.test(childKey)) return INVALID;
    const cleaned = sanitizeSectionData(child, childKey, depth + 1);
    if (cleaned === INVALID) return INVALID;
    result[childKey] = cleaned;
  }
  return result;
}

function parseSection(raw, partial) {
  if (!isPlainObject(raw)) return null;
  const id = validId(raw.id);
  if (!id) return null;
  const section = { id };
  if (!partial || raw.type != null) {
    if (!SECTION_TYPES.includes(raw.type)) return null;
    section.type = raw.type;
  }
  if (raw.heading != null) { const heading = boundedString(raw.heading, 120); if (heading == null) return null; section.heading = heading; }
  if (raw.status != null) { if (!SECTION_STATES.includes(raw.status)) return null; section.status = raw.status; }
  if (raw.reason != null) { const reason = boundedString(raw.reason, 200); if (reason == null) return null; section.reason = reason; }
  if (raw.data != null) {
    const data = sanitizeSectionData(raw.data);
    if (data === INVALID || !isPlainObject(data)) return null;
    section.data = data;
  }
  return section;
}

function parseSource(raw) {
  if (!isPlainObject(raw)) return null;
  const id = validId(raw.id);
  const name = boundedString(raw.name, 120);
  const url = sanitizeUrl(raw.url, 'url');
  if (!id || !name || !url) return null;
  const source = { id, name, url };
  for (const key of ['accessedAt', 'publishedAt']) {
    if (raw[key] != null) { const value = boundedString(raw[key], 40); if (value == null) return null; source[key] = value; }
  }
  return source;
}

function parsePresentation(source) {
  const presentationId = validId(source.presentationId);
  const requestId = validId(source.requestId);
  if (!presentationId || !requestId) return null;
  const action = { type: source.type, presentationId, requestId };
  if (source.turnId != null) { const turnId = validId(source.turnId); if (!turnId) return null; action.turnId = turnId; }
  if (source.status != null) { if (!PRESENTATION_STATES.includes(source.status)) return null; action.status = source.status; }
  if (source.activeSectionId != null) { const active = validId(source.activeSectionId); if (!active) return null; action.activeSectionId = active; }
  if (source.mode != null) { if (source.mode !== 'stage') return null; action.mode = 'stage'; }
  if (source.stage != null) { if (source.stage !== 'summary') return null; action.stage = 'summary'; }
  const partial = source.type === 'present_patch';
  if (!partial) {
    const title = boundedString(source.title, 120);
    if (!title) return null;
    action.title = title;
    action.status = action.status || 'preparing_query';
  }
  for (const [key, limit, parse] of [['sections', 12, (s) => parseSection(s, partial)], ['sources', 20, parseSource]]) {
    if (source[key] == null) { if (!partial) action[key] = []; continue; }
    if (!Array.isArray(source[key]) || source[key].length > limit) return null;
    const items = source[key].map(parse);
    if (items.includes(null)) return null;
    action[key] = items;
  }
  if (!partial && new Set(action.sections.map((s) => s.id)).size !== action.sections.length) return null;
  return action;
}

export const __testing = Object.freeze({ sanitizeUrl, sanitizeProps, byteLength, validVideoId: (value) => typeof value === 'string' && VIDEO_ID_RE.test(value.trim()) });
