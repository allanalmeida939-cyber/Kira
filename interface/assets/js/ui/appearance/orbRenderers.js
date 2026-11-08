// Orbes alternativos à galáxia (kira-3d.js): desenho em canvas 2D sem tocar no DOM, para dar para testar com
// node --test. Cada quadro recebe { t, w, h, state, level, accent }: t em segundos, level 0..1 (voz da Kira).
import {
  createBlackHoleRenderer, createConstellationRenderer, createDataRingsRenderer, createHiveRenderer, createPlasmaRenderer,
  createOrbStack, createRibbonsRenderer, createTorusRenderer,
} from './orbRenderersKira.js';
import { createCloudRenderer, createGeoHiveRenderer, createVoxelRenderer } from './orbRenderersV2.js';

export const ACCENTS = {
  roxo: { main: [168, 85, 247], soft: [240, 171, 252], listen: [110, 231, 255] },
  ciano: { main: [34, 211, 238], soft: [165, 243, 252], listen: [196, 181, 253] },
  rosa: { main: [236, 72, 153], soft: [251, 207, 232], listen: [110, 231, 255] },
  ambar: { main: [245, 158, 11], soft: [253, 230, 138], listen: [110, 231, 255] },
};
const ERROR = [248, 113, 113];
const ALPHA_STEPS = 6;
const TAU = Math.PI * 2;

function rgba([r, g, b], alpha) {
  return `rgba(${r},${g},${b},${Math.max(0, Math.min(1, alpha)).toFixed(3)})`;
}

function colorFor(state, accent) {
  if (state === 'error') return ERROR;
  return state === 'listening' ? accent.listen : accent.main;
}

// O brilho é um gradiente radial grande: refazê-lo a cada quadro era o item mais caro do orbe. Fica pronto numa imagem
// por cor/tamanho e só é carimbado com a opacidade do quadro.
const glowSprites = new Map();

function glowSprite(color, radius) {
  if (typeof OffscreenCanvas !== 'function') return null;
  const size = Math.max(8, Math.round(radius / 8) * 8);
  const key = `${color.join(',')}:${size}`;
  let sprite = glowSprites.get(key);
  if (!sprite) {
    if (glowSprites.size > 32) glowSprites.clear();
    sprite = new OffscreenCanvas(size * 2, size * 2);
    const g = sprite.getContext('2d');
    const gradient = g.createRadialGradient(size, size, 0, size, size, size);
    gradient.addColorStop(0, rgba(color, 1));
    gradient.addColorStop(1, rgba(color, 0));
    g.fillStyle = gradient;
    g.fillRect(0, 0, size * 2, size * 2);
    glowSprites.set(key, sprite);
  }
  return sprite;
}

function glow(ctx, cx, cy, radius, color, alpha) {
  const sprite = glowSprite(color, radius);
  if (sprite) {
    ctx.globalAlpha = Math.max(0, Math.min(1, alpha));
    ctx.drawImage(sprite, cx - radius, cy - radius, radius * 2, radius * 2);
    ctx.globalAlpha = 1;
    return;
  }
  const gradient = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
  gradient.addColorStop(0, rgba(color, alpha));
  gradient.addColorStop(1, rgba(color, 0));
  ctx.fillStyle = gradient;
  ctx.fillRect(cx - radius, cy - radius, radius * 2, radius * 2);
}

function ring(ctx, cx, cy, radius, from, to) {
  ctx.beginPath();
  ctx.arc(cx, cy, radius, from, to);
  ctx.stroke();
}

/** "Círculo": reator no estilo Jarvis. Anéis girando, barras de voz em volta do núcleo, onda ao ouvir. */
export function createRingRenderer() {
  return {
    draw(ctx, { t, w, h, state, level, accent }) {
      const cx = w / 2;
      const cy = h / 2;
      const R = Math.min(w, h) * 0.4;
      const color = colorFor(state, accent);
      const speaking = state === 'speaking';
      const thinking = state === 'thinking';
      const breath = 0.5 + 0.5 * Math.sin(t * (state === 'listening' ? 3.2 : 1.4));
      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = 'lighter';
      glow(ctx, cx, cy, R * 1.25, color, 0.1 + breath * 0.05 + (speaking ? level * 0.25 : 0));

      // Anel externo com marcações, girando devagar.
      ctx.strokeStyle = rgba(accent.soft, 0.32);
      ctx.lineWidth = 1;
      ring(ctx, cx, cy, R, 0, TAU);
      const spin = t * (thinking ? 0.35 : 0.06);
      for (const major of [true, false]) {
        ctx.strokeStyle = rgba(color, major ? 0.7 : 0.35);
        ctx.beginPath();
        for (let i = major ? 0 : 1; i < 72; i += major ? 6 : 1) {
          if (!major && i % 6 === 0) continue;
          const a = spin + (i / 72) * TAU;
          const inner = R - (major ? R * 0.07 : R * 0.03);
          ctx.moveTo(cx + Math.cos(a) * inner, cy + Math.sin(a) * inner);
          ctx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
        }
        ctx.stroke();
      }

      // Segmentos no sentido contrário; pensando, giram rápido.
      ctx.lineWidth = Math.max(2, R * 0.035);
      ctx.strokeStyle = rgba(color, 0.55 + (speaking ? level * 0.4 : 0));
      const counter = -t * (thinking ? 1.8 : 0.22);
      for (let i = 0; i < 6; i += 1) {
        const a = counter + (i / 6) * TAU;
        ring(ctx, cx, cy, R * 0.84, a, a + TAU / 6 - 0.22);
      }

      // Anel tracejado interno.
      ctx.lineWidth = 1.2;
      ctx.strokeStyle = rgba(accent.soft, 0.4);
      const dashSpin = t * 0.45;
      for (let i = 0; i < 24; i += 1) {
        const a = dashSpin + (i / 24) * TAU;
        ring(ctx, cx, cy, R * 0.66, a, a + 0.12);
      }

      if (thinking) {
        // Varredura: um arco claro dando voltas.
        ctx.lineWidth = Math.max(2, R * 0.025);
        ctx.strokeStyle = rgba(accent.soft, 0.85);
        const a = t * 3.4;
        ring(ctx, cx, cy, R * 0.75, a, a + 0.9);
      }

      if (speaking) {
        // Barras de voz em volta do núcleo, no ritmo da fala.
        ctx.lineWidth = Math.max(1.5, R * 0.018);
        for (const odd of [0, 1]) {
          ctx.strokeStyle = rgba(odd ? accent.soft : color, 0.45 + level * 0.5);
          ctx.beginPath();
          for (let i = odd; i < 64; i += 2) {
            const a = (i / 64) * TAU;
            const wave = 0.5 + 0.5 * Math.sin(i * 1.7 + t * 9) * Math.sin(i * 0.6 - t * 5);
            const length = R * (0.03 + level * 0.24 * wave);
            ctx.moveTo(cx + Math.cos(a) * R * 0.46, cy + Math.sin(a) * R * 0.46);
            ctx.lineTo(cx + Math.cos(a) * (R * 0.46 + length), cy + Math.sin(a) * (R * 0.46 + length));
          }
          ctx.stroke();
        }
      }

      if (state === 'listening') {
        // Ondas saindo do núcleo: "estou ouvindo".
        ctx.lineWidth = 1.5;
        for (let k = 0; k < 2; k += 1) {
          const progress = (t * 0.55 + k * 0.5) % 1;
          ctx.strokeStyle = rgba(color, (1 - progress) * 0.55);
          ring(ctx, cx, cy, R * (0.34 + progress * 0.5), 0, TAU);
        }
      }

      const core = R * (0.27 + breath * 0.025 + (speaking ? level * 0.12 : 0));
      const gradient = ctx.createRadialGradient(cx, cy, 0, cx, cy, core);
      gradient.addColorStop(0, 'rgba(255,255,255,0.95)');
      gradient.addColorStop(0.35, rgba(accent.soft, 0.75));
      gradient.addColorStop(0.75, rgba(color, 0.35));
      gradient.addColorStop(1, rgba(color, 0));
      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.arc(cx, cy, core, 0, TAU);
      ctx.fill();
      ctx.globalCompositeOperation = 'source-over';
      return { core };
    },
  };
}

/** "Partículas": esfera de pontos girando em 3D. Fala espalha os pontos, ouvir encolhe e cintila, pensar vira faixas. */
export function createParticleRenderer({ count = 520, orbiters = 36 } = {}) {
  const golden = Math.PI * (3 - Math.sqrt(5));
  const points = Array.from({ length: count }, (_, i) => {
    const y = 1 - (i / (count - 1)) * 2;
    const radius = Math.sqrt(1 - y * y);
    const theta = golden * i;
    return { x: Math.cos(theta) * radius, y, z: Math.sin(theta) * radius, seed: (i * 9301 + 49297) % 233280 / 233280 * TAU };
  });
  const moons = Array.from({ length: orbiters }, (_, i) => ({ angle: (i / orbiters) * TAU, speed: 0.25 + (i % 5) * 0.06 }));

  return {
    points,
    draw(ctx, { t, w, h, state, level, accent }) {
      const cx = w / 2;
      const cy = h / 2;
      const R = Math.min(w, h) * 0.3;
      const color = colorFor(state, accent);
      const speaking = state === 'speaking';
      const thinking = state === 'thinking';
      const listening = state === 'listening';
      const yaw = t * (thinking ? 0.9 : speaking ? 0.4 : 0.18) + (globalThis.kiraOrbView?.yaw || 0);
      const tilt = 0.38 + (globalThis.kiraOrbView?.pitch || 0);
      const cosY = Math.cos(yaw); const sinY = Math.sin(yaw);
      const cosX = Math.cos(tilt); const sinX = Math.sin(tilt);
      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = 'lighter';
      glow(ctx, cx, cy, R * 1.6, color, 0.12 + (speaking ? level * 0.3 : listening ? 0.08 : 0));
      let maxRadius = 0;
      // Pontos agrupados por cor e opacidade (ALPHA_STEPS níveis): troca de fillStyle por ponto custava ~8 µs cada.
      const buckets = Array.from({ length: ALPHA_STEPS * 2 }, () => []);
      for (const p of points) {
        let r = 1 + 0.02 * Math.sin(t * 1.2 + p.seed);
        if (speaking) r = 1 + level * 0.38 * (0.5 + 0.5 * Math.sin(p.y * 5 + t * 7 + p.seed));
        else if (listening) r = 0.9 + 0.03 * Math.sin(t * 5 + p.seed * 3);
        else if (thinking) r = 1 + 0.09 * Math.sin(p.y * 9 - t * 6);
        maxRadius = Math.max(maxRadius, r);
        const x1 = p.x * cosY - p.z * sinY;
        const z1 = p.x * sinY + p.z * cosY;
        const y2 = p.y * cosX - z1 * sinX;
        const z2 = p.y * sinX + z1 * cosX;
        const depth = (z2 + 1) / 2;
        const perspective = 1 / (1.6 - z2 * 0.35);
        const px = cx + x1 * r * R * perspective * 1.35;
        const py = cy + y2 * r * R * perspective * 1.35;
        const twinkle = listening ? 0.6 + 0.4 * Math.sin(t * 8 + p.seed * 5) : 1;
        const size = (0.8 + depth * 1.8) * (speaking ? 1 + level * 0.6 : 1);
        const step = Math.min(ALPHA_STEPS - 1, Math.floor((0.18 + depth * 0.75) * twinkle * ALPHA_STEPS));
        buckets[(depth > 0.55 ? ALPHA_STEPS : 0) + step].push(px - size / 2, py - size / 2, size);
      }
      buckets.forEach((rects, index) => {
        if (!rects.length) return;
        ctx.fillStyle = rgba(index >= ALPHA_STEPS ? accent.soft : color, ((index % ALPHA_STEPS) + 1) / ALPHA_STEPS);
        ctx.beginPath();
        for (let i = 0; i < rects.length; i += 3) ctx.rect(rects[i], rects[i + 1], rects[i + 2], rects[i + 2]);
        ctx.fill();
      });
      // Pontos em órbita, num anel inclinado.
      for (const moon of moons) {
        const a = moon.angle + t * moon.speed * (thinking ? 3 : 1);
        const px = cx + Math.cos(a) * R * 1.75;
        const py = cy + Math.sin(a) * R * 0.42;
        ctx.fillStyle = rgba(accent.soft, 0.35 + 0.3 * Math.sin(a));
        ctx.fillRect(px - 1, py - 1, 2, 2);
      }
      glow(ctx, cx, cy, R * (0.42 + (speaking ? level * 0.25 : 0)), [255, 255, 255], 0.55);
      ctx.globalCompositeOperation = 'source-over';
      return { maxRadius };
    },
  };
}

export const ORB_RENDERERS = {
  nuvem: createCloudRenderer,
  colmeia: createGeoHiveRenderer,
  voxels: createVoxelRenderer,
  ring: createRingRenderer,
  particles: createParticleRenderer,
  constellation: createConstellationRenderer,
  hive: createHiveRenderer,
  blackhole: createBlackHoleRenderer,
  datarings: createDataRingsRenderer,
  plasma: createPlasmaRenderer,
  torus: createTorusRenderer,
  ribbons: createRibbonsRenderer,
  fusion: () => createOrbStack(['rings', 'plasma', 'torus', 'ribbons']),
};
