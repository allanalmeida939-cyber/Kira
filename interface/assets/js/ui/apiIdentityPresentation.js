const SENSITIVE_KEY = /(?:key|token|secret|password|authorization|credential|private)/i;
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function cloneSafe(value, seen = new WeakSet()) {
  if (value == null || typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;
  if (typeof value !== 'object' || seen.has(value)) return undefined;
  seen.add(value);
  if (Array.isArray(value)) return value.map((item) => cloneSafe(item, seen)).filter((item) => item !== undefined);
  const result = {};
  Object.entries(value).forEach(([key, item]) => {
    if (SENSITIVE_KEY.test(key)) return;
    const safe = cloneSafe(item, seen);
    if (safe !== undefined) result[key] = safe;
  });
  return result;
}

export function maskEmail(value) {
  const text = String(value == null ? '' : value).trim();
  if (!text || !EMAIL_PATTERN.test(text)) return text;
  const [local, domain] = text.split('@');
  return `${local.slice(0, Math.min(2, local.length))}***@${domain}`;
}

export function sanitizeIdentitySnapshot(snapshot) {
  const safe = cloneSafe(snapshot);
  return safe && typeof safe === 'object' ? safe : {};
}

export function presentIdentityEmail(value) {
  const full = String(value == null ? '' : value).trim() || 'e-mail nao informado';
  return { full, masked: maskEmail(full) || 'e-mail nao informado' };
}

export const API_IDENTITY_SENSITIVE_KEY = SENSITIVE_KEY;
