export function create(documentRef, tag, className = '', text = '') {
  const node = documentRef.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function safeText(value, fallback = '') {
  const result = String(value == null ? '' : value).trim();
  return result || fallback;
}

export function safeHttps(value) {
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const url = new URL(value.trim());
    return url.protocol === 'https:' ? url.href : null;
  } catch (_) {
    return null;
  }
}

const PROXY_PATH = '/api/ui/img?u=';

/** Imagens so carregam pelo proxy local do backend (nunca direto de hosts externos). */
// radar do céu (services/sky_watch.py), servido pelo backend
const LOCAL_IMAGE_RE = /^\/api\/(sky\/image\/radar-[0-9a-f]{10}\.png|vision\/image\/cam-[0-9a-f]{10}\.jpg)$/;

export function safeImage(value) {
  if (typeof value === 'string' && LOCAL_IMAGE_RE.test(value)) return value;
  return typeof value === 'string' && value.startsWith(PROXY_PATH) && value.length <= 2000 ? value : null;
}

export function clear(node) {
  while (node?.firstChild) node.removeChild(node.firstChild);
}

export function disabledActionButton(documentRef, label) {
  const button = create(documentRef, 'button', 'kui-panel-button', label);
  button.type = 'button';
  button.disabled = true;
  button.title = 'indisponível nesta versão';
  return button;
}
