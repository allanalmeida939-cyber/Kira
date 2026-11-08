// Thinking Orbs (2026-11-02): 9 estados reais da Kira num só desenho Canvas 2D (sem WebGL). Serve de orbe NÚCLEO
// (estilo "thinking" na Aparência) e de mini-orbe <kira-orb state="buscando" size="28"> em cantos, cartões e
// carregamentos — um laço de animação só para todos, pausa fora da tela e respeita reduced-motion.

export const THINKING_STATES = Object.freeze({
  trabalhando: 'Trabalhando', buscando: 'Buscando', resolvendo: 'Resolvendo', ouvindo: 'Ouvindo', conectando: 'Conectando',
  trancando: 'Trançando', compondo: 'Compondo', respirando: 'Respirando', moldando: 'Moldando',
});
// estado da voz -> estado do orbe (quando nenhuma tarefa está em andamento)
const FROM_VOICE = { listening: 'ouvindo', thinking: 'resolvendo', speaking: 'compondo', idle: 'respirando', error: 'respirando' };
const TAU = Math.PI * 2;

/** Estado efetivo: tarefa em andamento (window.kiraActivity) ganha do estado da voz. */
export function thinkingState(voiceState) {
  const act = globalThis.kiraActivity;
  if (act && act.until > Date.now() && THINKING_STATES[act.state]) return act.state;
  return FROM_VOICE[voiceState] || 'respirando';
}

const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

/** Desenha um estado em ctx (w x h). level 0..1 = volume da voz. */
export function drawThinking(ctx, { t, w, h, state, level = 0, accent }) {
  const main = accent?.main || [168, 85, 247];
  const glow = accent?.glow || [236, 72, 153];
  const cx = w / 2; const cy = h / 2; const R = Math.min(w, h) * 0.34;
  ctx.clearRect(0, 0, w, h);
  const halo = ctx.createRadialGradient(cx, cy, R * 0.2, cx, cy, R * 1.5);
  halo.addColorStop(0, rgba(main, 0.35)); halo.addColorStop(1, rgba(main, 0));
  ctx.fillStyle = halo; ctx.fillRect(0, 0, w, h);
  ctx.lineCap = 'round';
  const dot = (x, y, r, a, c = main) => { ctx.fillStyle = rgba(c, a); ctx.beginPath(); ctx.arc(x, y, Math.max(0.6, r), 0, TAU); ctx.fill(); };
  const ring = (r, a, lw, from = 0, to = TAU, c = main) => { ctx.strokeStyle = rgba(c, a); ctx.lineWidth = lw; ctx.beginPath(); ctx.arc(cx, cy, r, from, to); ctx.stroke(); };
  const s = R / 40; // escala: o mini-orbe de 28 px e o núcleo de 400 px usam o mesmo desenho
  switch (state) {
    case 'trabalhando': // engrenagem: segmentos girando em dois sentidos
      for (let i = 0; i < 8; i += 1) { const a = t * 1.6 + (i * TAU) / 8; ring(R * 0.85, 0.85, 3 * s, a, a + 0.42); }
      for (let i = 0; i < 6; i += 1) { const a = -t * 2.4 + (i * TAU) / 6; ring(R * 0.5, 0.6, 2.4 * s, a, a + 0.5, glow); }
      break;
    case 'buscando': { // radar: varredura com rastro e pontos achados
      const a = t * 2.2;
      for (let k = 0; k < 14; k += 1) ring(R * 0.9 * (k / 14), 0.05, 1 * s);
      for (let k = 0; k < 18; k += 1) { const aa = a - k * 0.05; ctx.strokeStyle = rgba(main, 0.5 * (1 - k / 18)); ctx.lineWidth = 2 * s; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.cos(aa) * R, cy + Math.sin(aa) * R); ctx.stroke(); }
      for (let i = 0; i < 5; i += 1) { const pa = i * 1.7; const d = ((a - pa) % TAU + TAU) % TAU; dot(cx + Math.cos(pa) * R * (0.3 + i * 0.12), cy + Math.sin(pa) * R * (0.3 + i * 0.12), 2.6 * s, Math.max(0, 1 - d / 2), glow); }
      break;
    }
    case 'resolvendo': // partículas convergindo para o centro e recomeçando
      for (let i = 0; i < 24; i += 1) { const p = (t * 0.5 + i / 24) % 1; const a = i * 2.4 + t * 0.3; const r = R * (1 - p); dot(cx + Math.cos(a) * r, cy + Math.sin(a) * r, 2 * s * (1 - p * 0.5), 0.3 + p * 0.7, i % 3 ? main : glow); }
      dot(cx, cy, 5 * s * (1 + 0.2 * Math.sin(t * 6)), 0.9, glow);
      break;
    case 'ouvindo': // ondas concêntricas puxadas pelo volume da voz
      for (let k = 0; k < 4; k += 1) { const p = (t * 0.7 + k / 4) % 1; ring(R * (0.3 + p * 0.8) * (1 + level * 0.3), (1 - p) * 0.8, (2 + level * 4) * s); }
      dot(cx, cy, R * (0.22 + level * 0.15), 0.85);
      break;
    case 'conectando': { // nós num anel com linhas acendendo em sequência
      const n = 7; const pts = Array.from({ length: n }, (_, i) => [cx + Math.cos((i * TAU) / n + t * 0.3) * R * 0.85, cy + Math.sin((i * TAU) / n + t * 0.3) * R * 0.85]);
      const on = Math.floor(t * 3) % n;
      pts.forEach((p, i) => { const q = pts[(i + 2) % n]; ctx.strokeStyle = rgba(i === on ? glow : main, i === on ? 0.9 : 0.25); ctx.lineWidth = (i === on ? 2.4 : 1.2) * s; ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]); ctx.stroke(); dot(p[0], p[1], 3 * s, 0.9, i === on ? glow : main); });
      break;
    }
    case 'trancando': // duas fitas senoidais se cruzando
      for (const [ph, c] of [[0, main], [Math.PI, glow]]) {
        ctx.strokeStyle = rgba(c, 0.85); ctx.lineWidth = 3 * s; ctx.beginPath();
        for (let x = -R; x <= R; x += 2 * s) { const y = Math.sin(x / R * 3 + t * 3 + ph) * R * 0.45 * Math.cos(x / R * 1.5); x === -R ? ctx.moveTo(cx + x, cy + y) : ctx.lineTo(cx + x, cy + y); }
        ctx.stroke();
      }
      break;
    case 'compondo': // espiral de pontos surgindo (falando: pulsa com a voz)
      for (let i = 0; i < 40; i += 1) { const a = i * 0.5 + t * 1.2; const r = (i / 40) * R; const life = (t * 1.5 + i / 40) % 1; dot(cx + Math.cos(a) * r, cy + Math.sin(a) * r, (1.2 + level * 2.5) * s, 0.25 + 0.75 * life, i % 4 ? main : glow); }
      break;
    case 'moldando': { // bolha que muda de forma
      ctx.fillStyle = rgba(main, 0.7); ctx.beginPath();
      for (let k = 0; k <= 48; k += 1) { const a = (k / 48) * TAU; const r = R * (0.7 + 0.12 * Math.sin(a * 3 + t * 2) + 0.08 * Math.sin(a * 5 - t * 3)); k ? ctx.lineTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r) : ctx.moveTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r); }
      ctx.fill(); ring(R * 0.9, 0.35, 1.5 * s, 0, TAU, glow);
      break;
    }
    default: { // respirando: esfera calma que cresce e diminui
      const b = 0.75 + 0.1 * Math.sin(t * 1.4);
      const g = ctx.createRadialGradient(cx - R * 0.2, cy - R * 0.2, R * 0.1, cx, cy, R * b);
      g.addColorStop(0, rgba(glow, 0.9)); g.addColorStop(0.6, rgba(main, 0.6)); g.addColorStop(1, rgba(main, 0));
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, R * b, 0, TAU); ctx.fill();
    }
  }
}

/** Orbe núcleo (Aparência > Orbe > Thinking Orbs): mesmo contrato dos outros renderers. */
export function createThinkingRenderer() {
  return { draw(ctx, o) { drawThinking(ctx, { ...o, state: thinkingState(o.state) }); } };
}

// ---- mini-orbes: <kira-orb state="buscando" size="28" label> (state vazio = segue a Kira) ----
const minis = new Set();
let raf = 0;
const still = () => globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
function loop(now) {
  raf = 0;
  for (const el of minis) {
    // oculto (display:none / [hidden]) não pinta: economiza GPU sem parar os visíveis
    if (!el.isConnected || el.offsetParent === null) continue;
    el.paint(now / 1000);
  }
  if (minis.size && !globalThis.document?.hidden) raf = requestAnimationFrame(loop);
}
const wake = () => { if (!raf && minis.size && !globalThis.document?.hidden) raf = requestAnimationFrame(loop); };
globalThis.document?.addEventListener?.('visibilitychange', wake);

if (globalThis.customElements && !customElements.get('kira-orb')) {
  customElements.define('kira-orb', class extends HTMLElement {
    connectedCallback() {
      const size = Number(this.getAttribute('size')) || 28;
      if (!this.canvas) {
        this.canvas = document.createElement('canvas');
        this.canvas.width = this.canvas.height = Math.round(size * Math.min(2, devicePixelRatio || 1));
        Object.assign(this.canvas.style, { width: `${size}px`, height: `${size}px`, display: 'block' });
        this.append(this.canvas);
        this.style.display = 'inline-flex'; this.style.alignItems = 'center'; this.style.gap = '6px';
        if (this.hasAttribute('label')) { this.text = document.createElement('span'); this.text.className = 'kira-orb-label'; this.append(this.text); }
      }
      minis.add(this); wake();
    }
    disconnectedCallback() { minis.delete(this); } // limpeza: sai do laço ao sair da página
    paint(t) {
      const state = this.getAttribute('state') || thinkingState(globalThis.kiraVoiceState);
      if (this.text) this.text.textContent = this.getAttribute('label') || THINKING_STATES[state] || '';
      const ctx = this.canvas.getContext('2d');
      this.dataset.state = state;
      const accent = globalThis.kiraAccent;
      drawThinking(ctx, { t: still() ? t * 0.35 : t, w: this.canvas.width, h: this.canvas.height, state, level: globalThis.kiraVoiceLevel || 0, accent });
    }
  });
}
