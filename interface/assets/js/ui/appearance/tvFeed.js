// Orbe da TV (2026-11-06): só com a página da TV viva, manda o estado da Kira e o nível da voz para o backend
// (POST /api/tv/state -> SSE da TV). Falando ~8x/s; fora disso só quando o estado muda. Parado custa um GET a cada 4 s.
const FULL = 0.3; // mesmo fundo de escala do appearance.js: 0,3 já é voz cheia
let alive = false;
let last = '';

async function check() {
  try { alive = Boolean((await (await fetch('/api/tv/status', { cache: 'no-store' })).json()).alive); } catch (_) { alive = false; }
}

function send(state, level) {
  fetch('/api/tv/state', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ state, level }), keepalive: true }).catch(() => {});
}

function tick() {
  if (alive) {
    const state = globalThis.kiraVoiceState || 'idle';
    const level = state === 'speaking' ? Math.min(1, (Number(globalThis.kiraVoiceLevel) || 0) / FULL) : 0;
    if (state !== last || state === 'speaking') send(state === 'error' ? 'idle' : state, level);
    last = state;
  }
  setTimeout(tick, alive ? 125 : 1000);
}

if (globalThis.document && globalThis.fetch && !globalThis.__kiraTvFeed) {
  globalThis.__kiraTvFeed = true;
  check();
  setInterval(check, 4000);
  tick();
}
