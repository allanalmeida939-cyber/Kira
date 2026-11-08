// Orbes escolhidos pelo Chefe (2026-11-05): Nuvem de Partículas, Colmeia Geodésica e Voxels.
// Canvas 2D, mesmo contrato dos outros: draw(ctx, { t, w, h, state, level, accent }). Cores agrupadas em poucos
// fillStyle por quadro (trocar de cor por ponto era o que mais custava).
const TAU = Math.PI * 2;
const STEPS = 6;
const rgba = ([r, g, b], a) => `rgba(${r},${g},${b},${Math.max(0, Math.min(1, a)).toFixed(3)})`;
const fib = (n) => Array.from({ length: n }, (_, i) => {
  const y = 1 - (2 * (i + 0.5)) / n; const r = Math.sqrt(1 - y * y); const a = i * 2.39996;
  return [r * Math.cos(a), y, r * Math.sin(a), (i * 7919 % 1000) / 1000];
});
function colorOf(state, accent) { return state === 'error' ? [248, 113, 113] : state === 'listening' ? accent.listen : accent.main; }
// pseudo nível de escuta (o microfone não tem analisador próprio): respiração viva, não parada
const listenLevel = () => 0.12; // ouvindo: quieto (a pulsação falsa fazia o orbe reagir o tempo todo, 2026-11-07)
function view(t, state) {
  const v = globalThis.kiraOrbView || {};
  const yaw = t * (state === 'thinking' ? 1.0 : 0.25) + (v.yaw || 0);
  const tilt = 0.32 + (v.pitch || 0);
  return [Math.cos(yaw), Math.sin(yaw), Math.cos(tilt), Math.sin(tilt)];
}
function project(x, y, z, [cy, sy, cx, sx]) {
  const x1 = x * cy - z * sy; const z1 = x * sy + z * cy;
  return [x1, y * cx - z1 * sx, y * sx + z1 * cx];
}
function begin(ctx, w, h) { ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, w, h); return [w / 2, h / 2, Math.min(w, h) * 0.3]; }
// brilho pronto numa imagem por cor (antes: gradiente novo a cada quadro, o item mais caro do orbe)
const haloCache = new Map();
function halo(ctx, cx, cy, r, color, a) {
  const key = color.join(',');
  let img = haloCache.get(key);
  if (!img && typeof OffscreenCanvas === 'function') {
    img = new OffscreenCanvas(128, 128); const g = img.getContext('2d'); const gr = g.createRadialGradient(64, 64, 0, 64, 64, 64);
    gr.addColorStop(0, rgba(color, 1)); gr.addColorStop(1, rgba(color, 0)); g.fillStyle = gr; g.fillRect(0, 0, 128, 128);
    haloCache.set(key, img);
  }
  if (!img) return;
  ctx.globalAlpha = Math.max(0, Math.min(1, a)); ctx.drawImage(img, cx - r, cy - r, r * 2, r * 2); ctx.globalAlpha = 1;
}

/** Nuvem: falar espalha os pontos como som; ouvir recolhe e cintila; trabalhar passa faixas de luz. */
export function createCloudRenderer({ count = 900 } = {}) {
  const pts = fib(count);
  return { draw(ctx, { t, w, h, state, level, accent }) {
    const [cx, cy, R] = begin(ctx, w, h); const col = colorOf(state, accent); const rot = view(t, state);
    const e = state === 'speaking' ? level : state === 'listening' ? listenLevel(t) : 0;
    ctx.globalCompositeOperation = 'lighter';
    halo(ctx, cx, cy, R * 1.5, col, 0.1 + e * 0.25);
    const buckets = Array.from({ length: STEPS }, () => []);
    for (const [x, y, z, s] of pts) {
      let k = 1 + 0.015 * Math.sin(t * 1.3 + s * TAU);
      if (state === 'speaking') k += e * 0.32 * Math.sin(x * 6 + t * 4) * Math.sin(y * 5 + t * 3 + s);
      else if (state === 'listening') k -= 0.06 + e * 0.04 * Math.sin(t * 9 + s * 20);
      else if (state === 'thinking') k += 0.07 * Math.sin(y * 18 - t * 7);
      const [px, py, pz] = project(x * k, y * k, z * k, rot);
      const d = (pz + 1) / 2; const size = 0.9 + d * 1.9 + (state === 'speaking' ? e * 1.2 : 0);
      buckets[Math.min(STEPS - 1, Math.floor((0.15 + d * 0.85) * STEPS))].push(cx + px * R * 1.25, cy + py * R * 1.25, size);
    }
    buckets.forEach((b, i) => { if (!b.length) return; ctx.fillStyle = rgba(i > 3 ? accent.soft : col, (i + 1) / STEPS); ctx.beginPath(); for (let j = 0; j < b.length; j += 3) ctx.rect(b[j], b[j + 1], b[j + 2], b[j + 2]); ctx.fill(); });
    halo(ctx, cx, cy, R * (0.45 + e * 0.3), [255, 255, 255], 0.35 + e * 0.3);
    ctx.globalCompositeOperation = 'source-over';
    return { maxRadius: 1.35 };
  } };
}

/** Colmeia: hexágonos na esfera; falar acende células, ouvir pulsa azul, trabalhar faz uma varredura. */
export function createGeoHiveRenderer({ count = 420 } = {}) {
  const pts = fib(count);
  const hex = Array.from({ length: 6 }, (_, k) => [Math.cos((k * TAU) / 6), Math.sin((k * TAU) / 6)]);
  return { draw(ctx, { t, w, h, state, level, accent }) {
    const [cx, cy, R] = begin(ctx, w, h); const col = colorOf(state, accent); const rot = view(t, state);
    const e = state === 'speaking' ? level : state === 'listening' ? listenLevel(t) : 0;
    const scan = Math.sin(t * 2.2);
    const buckets = Array.from({ length: STEPS }, () => []);
    for (const [x, y, z, s] of pts) {
      const [px, py, pz] = project(x, y, z, rot);
      if (pz < -0.15) continue;
      let lit = 0.08;
      if (state === 'speaking') lit = e * (Math.sin(s * 80 + t * 11) > 0.25 ? 1 : 0.25);
      else if (state === 'listening') lit = 0.15 + e * 0.3 * (0.5 + 0.5 * Math.sin(t * 6 + pz * 6));
      else if (state === 'thinking') lit = Math.max(0.1, 1 - Math.abs(py - scan) * 4);
      buckets[Math.min(STEPS - 1, Math.floor(lit * (STEPS - 0.01)))].push(px, py, pz);
    }
    ctx.globalCompositeOperation = 'lighter';
    halo(ctx, cx, cy, R * 1.4, col, 0.08 + e * 0.2);
    buckets.forEach((b, i) => {
      if (!b.length) return;
      ctx.beginPath();
      for (let j = 0; j < b.length; j += 3) {
        const sz = R * 0.075 * (0.45 + b[j + 2] * 0.55); const X = cx + b[j] * R * 1.2; const Y = cy + b[j + 1] * R * 1.2;
        ctx.moveTo(X + hex[0][0] * sz, Y + hex[0][1] * sz);
        for (let k = 1; k < 6; k += 1) ctx.lineTo(X + hex[k][0] * sz, Y + hex[k][1] * sz);
        ctx.closePath();
      }
      ctx.fillStyle = rgba(i >= 4 ? accent.soft : col, 0.12 + (i / STEPS) * 0.8); ctx.fill();
      ctx.strokeStyle = rgba(accent.soft, 0.25); ctx.lineWidth = 0.6; ctx.stroke();
    });
    ctx.globalCompositeOperation = 'source-over';
    return { maxRadius: 1.25 };
  } };
}

/** Voxels: cubinhos numa grade de latitude/longitude que saltam pra fora com a voz. */
export function createVoxelRenderer({ lat = 22, lon = 44 } = {}) {
  const cells = [];
  for (let a = 1; a < lat; a += 1) for (let b = 0; b < lon; b += 1) {
    const la = -Math.PI / 2 + (Math.PI * a) / lat; const lo = (b * TAU) / lon;
    cells.push([Math.cos(la) * Math.cos(lo), Math.sin(la), Math.cos(la) * Math.sin(lo), a, b]);
  }
  return { draw(ctx, { t, w, h, state, level, accent }) {
    const [cx, cy, R] = begin(ctx, w, h); const col = colorOf(state, accent); const rot = view(t, state);
    const e = state === 'speaking' ? level : state === 'listening' ? listenLevel(t) : 0;
    const buckets = Array.from({ length: STEPS }, () => []);
    for (const [x, y, z, a, b] of cells) {
      let pop = 0;
      if (state === 'speaking') pop = e * 0.32 * Math.max(0, Math.sin(a * 0.9 + b * 0.5 - t * 8));
      else if (state === 'listening') pop = e * 0.08 * Math.max(0, Math.sin(a * 0.6 - t * 5));
      else if (state === 'thinking') pop = 0.14 * Math.max(0, Math.sin(b * 0.5 - t * 6));
      const k = 1 + pop; const [px, py, pz] = project(x * k, y * k, z * k, rot);
      if (pz < -0.2) continue;
      const s = (2.2 + pz * 2.6 + pop * 14) * (R / 120);
      buckets[Math.min(STEPS - 1, Math.floor(((pz + 1) / 2 * 0.6 + pop * 2.5) * STEPS))].push(cx + px * R * 1.2 - s / 2, cy + py * R * 1.2 - s / 2, s);
    }
    ctx.globalCompositeOperation = 'lighter';
    halo(ctx, cx, cy, R * 1.4, col, 0.08 + e * 0.2);
    buckets.forEach((bk, i) => { if (!bk.length) return; ctx.fillStyle = rgba(i >= 4 ? accent.soft : col, 0.25 + (i / STEPS) * 0.75); ctx.beginPath(); for (let j = 0; j < bk.length; j += 3) ctx.rect(bk[j], bk[j + 1], bk[j + 2], bk[j + 2]); ctx.fill(); });
    ctx.globalCompositeOperation = 'source-over';
    return { maxRadius: 1.4 };
  } };
}
