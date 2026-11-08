/**
 * Galáxia da Kira: o desenho do orbe.
 *
 * Cada brilho (estrela, braço da espiral, nó, núcleo) é um carimbo pronto num atlas, feito uma vez em memória. A cada
 * quadro só se calcula onde vai cada carimbo e com que transparência, e o WebGL desenha tudo de uma vez: 2 comandos
 * para a placa de vídeo por quadro, em vez de ~650.
 *
 * Por que assim (2026-10-25, medido na Kira): o desenho antigo criava ~330 gradientes e usava sombra borrada a cada
 * quadro, ~15 ms na linha da página, e por isso rodava a 10 quadros parado e 30 falando. Canvas acelerado em worker
 * derrubou a Kira no mesmo ponto do Qt6WebEngineCore (+0xfc51b3) das quedas de 2026-10-21 (a GTX 550 Ti está na
 * lista de bloqueio do Chromium para pintura na placa, e a placa roda dentro do python.exe). Pintar na CPU também não
 * serve: no motor da Kira, misturar em "screen" na CPU custa ~55 ms num brilho grande. WebGL é o caminho que o globo
 * do Olho de Deus já usa nesta máquina. Sem WebGL, o mesmo atlas é desenhado por um canvas 2D (como antes).
 */
(function (root) {
  "use strict";

  const TAU = Math.PI * 2;
  const DEFAULT_CONFIG = {
    rotationSpeed: 0.018,
    armCount: 3,
    starCount: 380,
    dustOpacity: 0.72,
    glowIntensity: 1.35,
    voiceGlowMult: 3.2,
    nucleusSize: 0.072,
    diskFlat: 0.42,
  };
  const PROFILES = {
    idle: { glowMult: 1.0, rotSpeed: 1.0, diskAlpha: 0.72, coreInner: "#f5d0fe", coreMid: "#e879f9", coreGlow: "#7c3aed", aura: "#a855f7", pulseFreq: 1.4 },
    listening: { glowMult: 1.12, rotSpeed: 1.06, diskAlpha: 0.78, coreInner: "#e9d5ff", coreMid: "#c084fc", coreGlow: "#6d28d9", aura: "#8b5cf6", pulseFreq: 1.6 },
    thinking: { glowMult: 1.24, rotSpeed: 1.18, diskAlpha: 0.82, coreInner: "#f5d0fe", coreMid: "#e879f9", coreGlow: "#9333ea", aura: "#a855f7", pulseFreq: 2.0 },
    speaking: { glowMult: 1.0, rotSpeed: 1.0, diskAlpha: 0.8, coreInner: "#f5d0fe", coreMid: "#e879f9", coreGlow: "#f0abfc", aura: "#a855f7", pulseFreq: 2.8 },
    error: { glowMult: 0.88, rotSpeed: 0.75, diskAlpha: 0.58, coreInner: "#fecaca", coreMid: "#f87171", coreGlow: "#ef4444", aura: "#7f1d1d", pulseFreq: 1.0 },
  };
  // Tons dos braços, nós e sinais (eram contínuos): de 6 em 6 graus a diferença não aparece e os carimbos ficam poucos.
  const HUE_STEP = 6;
  const ATLAS_SIZE = 2048;
  // Carimbos são pintados em memória (uma vez); só a imagem pronta vai para a placa.
  const SOFTWARE = { alpha: true, willReadFrequently: true };
  // x, y, meia largura, meia altura, rotação, transparência, u0, v0, u1, v1
  const STRIDE = 10;
  // 30 quadros no PC (cada quadro custa CPU na janela da Kira); no celular, 60: a placa dele dá conta e fica mais fluido
  // limite escolhido em Aparência > Animações ('seguro' = 30; 60/100 para placas que aguentam)
  const frameMsNow = () => { let c = 'seguro'; try { c = localStorage.getItem('kira.fps') || 'seguro'; } catch (_) {} return 1000 / (c === 'seguro' ? 30 : Number(c)); };

  function makeCanvas(width, height) {
    if (typeof OffscreenCanvas === "function") return new OffscreenCanvas(width, height);
    const canvas = root.document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    return canvas;
  }

  function clamp01(value) {
    return Math.max(0, Math.min(1, Number(value) || 0));
  }

  function rgba(hex, alpha) {
    const clean = String(hex || "#ffffff").replace("#", "").padEnd(6, "f").slice(0, 6);
    const value = parseInt(clean, 16);
    return `rgba(${(value >> 16) & 255},${(value >> 8) & 255},${value & 255},${clamp01(alpha)})`;
  }

  function hsl(hue, saturation, lightness, alpha) {
    return `hsla(${hue},${saturation}%,${lightness}%,${clamp01(alpha)})`;
  }

  function hueBucket(hue) {
    return Math.round(hue / HUE_STEP) * HUE_STEP;
  }

  /** Atlas de carimbos: cada um é um retângulo {x, y, w, h, u0, v0, u1, v1, extent}. version muda a cada carimbo novo. */
  class Atlas {
    constructor() {
      this.canvas = makeCanvas(ATLAS_SIZE, ATLAS_SIZE);
      this.ctx = this.canvas.getContext("2d", SOFTWARE);
      this.items = new Map();
      this.cursor = { x: 1, y: 1, row: 0 };
      this.version = 0;
      this.full = false;
    }

    /** Recomeça (tamanho do orbe mudou: anéis e ligações dependem dele). */
    reset() {
      this.ctx.clearRect(0, 0, ATLAS_SIZE, ATLAS_SIZE);
      this.items.clear();
      this.cursor = { x: 1, y: 1, row: 0 };
      this.version += 1;
      this.full = false;
    }

    get(key) {
      return this.items.get(key);
    }

    /** Pinta um carimbo novo (paint recebe o contexto já no canto do carimbo). Sem espaço: null (não desenha). */
    add(key, width, height, paint, extent = 1) {
      const w = Math.max(1, Math.ceil(width));
      const h = Math.max(1, Math.ceil(height));
      const cursor = this.cursor;
      if (cursor.x + w + 2 > ATLAS_SIZE) {
        cursor.x = 1;
        cursor.y += cursor.row + 2;
        cursor.row = 0;
      }
      if (cursor.y + h + 2 > ATLAS_SIZE) {
        if (!this.full) console.warn("[KIRA ORB] atlas cheio:", key);
        this.full = true;
        return null;
      }
      const x = cursor.x;
      const y = cursor.y;
      cursor.x += w + 2;
      cursor.row = Math.max(cursor.row, h);
      const ctx = this.ctx;
      ctx.save();
      ctx.beginPath();
      ctx.rect(x, y, w, h);
      ctx.clip();
      ctx.translate(x, y);
      paint(ctx, w, h);
      ctx.restore();
      const sprite = { x, y, w, h, u0: x / ATLAS_SIZE, v0: y / ATLAS_SIZE, u1: (x + w) / ATLAS_SIZE, v1: (y + h) / ATLAS_SIZE, extent };
      this.items.set(key, sprite);
      this.version += 1;
      return sprite;
    }

    /** Brilho radial: stops com as cores na transparência relativa (a do momento vem na hora de desenhar). */
    glow(key, size, stops, innerFrac = 0) {
      return this.get(key) || this.add(key, size, size, (ctx) => {
        const r = size / 2;
        const gradient = ctx.createRadialGradient(r, r, r * innerFrac, r, r, r);
        stops.forEach(([offset, color]) => gradient.addColorStop(offset, color));
        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.arc(r, r, r, 0, TAU);
        ctx.fill();
      });
    }

    /** Ponto branco; com blur, a sombra colorida vai junto. extent: metade do carimbo em raios do ponto. */
    dot(key, radius, blur = 0, blurColor = "") {
      const found = this.get(key);
      if (found) return found;
      const margin = blur ? blur * 1.6 : 1;
      const size = Math.ceil((radius + margin) * 2);
      return this.add(key, size, size, (ctx) => {
        ctx.fillStyle = "#ffffff";
        if (blur) {
          ctx.shadowBlur = blur;
          ctx.shadowColor = blurColor;
        }
        ctx.beginPath();
        ctx.arc(size / 2, size / 2, radius, 0, TAU);
        ctx.fill();
      }, size / 2 / radius);
    }
  }

  /** Lista dos carimbos do quadro, num Float32Array que vai direto para a placa. */
  class Instances {
    constructor() {
      this.data = new Float32Array(1024 * STRIDE);
      this.count = 0;
    }

    push(x, y, hx, hy, rot, sprite, alpha) {
      if (!sprite || !(alpha > 0.003) || !(hx > 0) || !(hy > 0)) return;
      if ((this.count + 1) * STRIDE > this.data.length) {
        const bigger = new Float32Array(this.data.length * 2);
        bigger.set(this.data);
        this.data = bigger;
      }
      const d = this.data;
      const o = this.count * STRIDE;
      d[o] = x;
      d[o + 1] = y;
      d[o + 2] = hx;
      d[o + 3] = hy;
      d[o + 4] = rot;
      d[o + 5] = alpha > 1 ? 1 : alpha;
      d[o + 6] = sprite.u0;
      d[o + 7] = sprite.v0;
      d[o + 8] = sprite.u1;
      d[o + 9] = sprite.v1;
      this.count += 1;
    }
  }

  /** A galáxia: partículas sorteadas uma vez e, a cada quadro, a lista de carimbos (todos somam em "screen"). */
  class OrbScene {
    constructor(config) {
      this.config = { ...DEFAULT_CONFIG, ...(config || {}) };
      this.pointer = { x: 0, y: 0 };
      this.metrics = null;
      this.bounds = null;
      this.offset = { x: 0, y: 0 };
      this.seed();
    }

    configure(config) {
      this.config = { ...this.config, ...(config || {}) };
    }

    seed() {
      this.diskStars = Array.from({ length: 120 }, () => {
        const hue = Math.random() < 0.55 ? 270 : (Math.random() < 0.7 ? 220 : 300);
        return {
          a: Math.random() * TAU,
          r: Math.pow(Math.random(), 0.65),
          sz: 0.3 + Math.random() * 1.2,
          al: 0.10 + Math.random() * 0.45,
          tw: Math.random() * TAU,
          tws: 0.15 + Math.random() * 0.75,
          hue,
          key: `disk:${hue}`,
          drift: (Math.random() - 0.5) * 0.0004,
        };
      });
      this.outerStars = Array.from({ length: 70 }, () => {
        const big = Math.random() < 0.18;
        const med = !big && Math.random() < 0.30;
        const hue = Math.random() < 0.55 ? 270 : (Math.random() < 0.55 ? 220 : 310);
        return {
          a: Math.random() * TAU,
          r: 0.50 + Math.random() * 0.48,
          sz: big ? (3 + Math.random() * 4) : med ? (1.2 + Math.random() * 1.8) : (0.3 + Math.random() * 0.9),
          al: big ? (0.55 + Math.random() * 0.38) : med ? (0.25 + Math.random() * 0.40) : (0.12 + Math.random() * 0.32),
          tw: Math.random() * TAU,
          tws: 0.06 + Math.random() * 0.50,
          hue,
          key: `outer:${hue}:${big ? 1 : 0}`,
          big,
          med,
          drift: (Math.random() - 0.5) * 0.00012,
        };
      });
      this.nebulas = [
        { x: -0.35, y: 0.10, r: 0.28, h: 270, a: 0.32 },
        { x: 0.38, y: -0.12, r: 0.22, h: 285, a: 0.26 },
        { x: 0.15, y: 0.26, r: 0.18, h: 260, a: 0.20 },
        { x: -0.12, y: -0.30, r: 0.16, h: 275, a: 0.24 },
      ];
      const armCount = 3;
      this.armPoints = [];
      for (let arm = 0; arm < armCount; arm += 1) {
        const offset = (TAU / armCount) * arm;
        for (let i = 0; i < 45; i += 1) {
          const frac = i / 44;
          const hue = hueBucket(258 + Math.random() * 45);
          this.armPoints.push({
            angle: offset + frac * TAU * 1.9 + (Math.random() - 0.5) * 0.032,
            r: Math.min(0.70, 0.20 + frac * 0.48),
            sz: 4 + Math.random() * 5,
            al: (0.45 + Math.random() * 0.35) * (1 - frac * 0.38),
            hue,
            key: `arm:${hue}`,
            phase: Math.random() * TAU,
          });
        }
      }
      // ry de cada anel sai do achatamento atual (a aba Aparência muda o achatamento ao vivo)
      this.rings = [
        { rx: 0.88, tilt: 0.00, speed: 0.10, op: 0.38, w: 1.3, c: "#8b5cf6" },
        { rx: 0.72, tilt: 0.16, speed: -0.08, op: 0.30, w: 1.0, c: "#9333ea" },
        { rx: 0.56, tilt: -0.12, speed: 0.06, op: 0.24, w: 0.9, c: "#a855f7" },
        { rx: 0.42, tilt: 0.08, speed: -0.05, op: 0.20, w: 0.7, c: "#c084fc" },
        { rx: 0.80, tilt: 0.24, speed: 0.04, op: 0.15, w: 0.5, c: "#7c3aed" },
      ];
      this.ringDots = [
        { ring: 0, angle: 0.0, sz: 5.0, al: 0.88, c: "#f0e6ff" },
        { ring: 0, angle: Math.PI, sz: 4.2, al: 0.80, c: "#ddd6fe" },
        { ring: 1, angle: Math.PI * 0.5, sz: 4.5, al: 0.82, c: "#e9d5ff" },
        { ring: 1, angle: Math.PI * 1.5, sz: 3.5, al: 0.72, c: "#c4b5fd" },
        { ring: 2, angle: Math.PI * 0.8, sz: 3.8, al: 0.75, c: "#f5d0fe" },
        { ring: 2, angle: Math.PI * 1.8, sz: 3.0, al: 0.65, c: "#ddd6fe" },
        { ring: 3, angle: Math.PI * 0.3, sz: 3.2, al: 0.70, c: "#e9d5ff" },
        { ring: 3, angle: Math.PI * 1.3, sz: 2.6, al: 0.60, c: "#c4b5fd" },
      ].map((dot) => ({ ...dot, glowKey: `ringdot:${dot.c}`, coreKey: `ringcore:${dot.c}:${dot.sz}` }));
      const rawNodes = [
        { x: 0.00, y: 0.00, s: 4.5, isCore: true },
        { x: -0.20, y: -0.16, s: 2.6 }, { x: 0.22, y: -0.18, s: 2.2 },
        { x: 0.20, y: 0.18, s: 2.4 }, { x: -0.16, y: 0.22, s: 2.0 },
        { x: -0.52, y: -0.28, s: 1.8 }, { x: 0.04, y: -0.54, s: 2.0 },
        { x: 0.50, y: -0.26, s: 1.6 }, { x: 0.54, y: 0.22, s: 1.8 },
        { x: 0.06, y: 0.52, s: 1.6 }, { x: -0.50, y: 0.26, s: 2.0 },
        { x: -0.70, y: -0.12, s: 1.4 }, { x: -0.38, y: -0.62, s: 1.6 },
        { x: 0.28, y: -0.66, s: 1.4 }, { x: 0.68, y: -0.36, s: 1.4 },
        { x: 0.70, y: 0.34, s: 1.2 }, { x: 0.34, y: 0.66, s: 1.6 },
        { x: -0.26, y: 0.70, s: 1.4 }, { x: -0.66, y: 0.36, s: 1.4 },
        { x: -0.58, y: -0.46, s: 1.2 }, { x: 0.56, y: -0.50, s: 1.3 },
        { x: 0.60, y: 0.48, s: 1.1 }, { x: -0.56, y: 0.48, s: 1.2 },
      ];
      this.neuralNodes = rawNodes
        .filter((node) => Math.sqrt(node.x * node.x + node.y * node.y) <= 0.88)
        .map((node) => {
          const hue = node.isCore ? 280 : hueBucket(252 + Math.random() * 68);
          return {
            ...node,
            phase: Math.random() * TAU,
            pulseSpeed: 0.35 + Math.random() * 0.80,
            hue,
            haloKey: `node:${hue}:${node.isCore ? 1 : 0}`,
          };
        });
      this.neuralConns = [];
      const addConn = (a, b, toCore = false) => {
        if (this.neuralConns.find((conn) => (conn.a === a && conn.b === b) || (conn.a === b && conn.b === a))) return;
        const signalHue = hueBucket(258 + Math.random() * 65);
        this.neuralConns.push({
          a,
          b,
          toCore,
          key: `conn:${this.neuralConns.length}`,
          phase: Math.random() * TAU,
          pulseSpeed: 0.20 + Math.random() * 0.40,
          hasSignal: Math.random() < 0.50,
          signalStart: Math.random(),
          signalSpeed: 0.050 + Math.random() * 0.08,
          signalHue,
          signalKey: `signal:${signalHue}`,
        });
      };
      [1, 2, 3, 4].forEach((target) => {
        if (target < this.neuralNodes.length) addConn(0, target, true);
      });
      const connCount = new Array(this.neuralNodes.length).fill(0);
      for (let i = 1; i < this.neuralNodes.length; i += 1) {
        if (connCount[i] >= 1) continue;
        const node = this.neuralNodes[i];
        let bestIndex = -1;
        let bestDist = Infinity;
        for (let j = 1; j < this.neuralNodes.length; j += 1) {
          if (i === j || connCount[j] >= 1) continue;
          if (this.neuralConns.find((conn) => (conn.a === i && conn.b === j) || (conn.a === j && conn.b === i))) continue;
          const dx = node.x - this.neuralNodes[j].x;
          const dy = node.y - this.neuralNodes[j].y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist > 0.25 && dist < 0.75 && dist < bestDist) {
            bestDist = dist;
            bestIndex = j;
          }
        }
        if (bestIndex >= 0) {
          addConn(i, bestIndex, false);
          connCount[i] += 1;
          connCount[bestIndex] += 1;
        }
      }
    }

    profile(state, audio) {
      const base = PROFILES[state] || PROFILES.idle;
      if (state !== "speaking") return base;
      return {
        ...base,
        glowMult: 1.0 + audio * this.config.voiceGlowMult,
        rotSpeed: 1.0 + audio * 0.55,
        diskAlpha: 0.80 + audio * 0.18,
        pulseFreq: 2.8 + audio * 3.5,
      };
    }

    /** Disco e nebulosas (só mudam com o tamanho e o achatamento): pintados uma vez, com o brilho do disco em 1. */
    paintDisk(canvas, width, height, dpr) {
      const ctx = canvas.getContext("2d", SOFTWARE);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      const size = Math.min(width, height);
      const cx = width * 0.5;
      const cy = height * 0.5;
      ctx.save();
      ctx.translate(cx, cy);
      ctx.scale(1, this.config.diskFlat);
      ctx.translate(-cx, -cy);
      const diskGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, size * 0.44);
      diskGrad.addColorStop(0, rgba("#ffffff", 0.56));
      diskGrad.addColorStop(0.10, rgba("#e9d5ff", 0.46));
      diskGrad.addColorStop(0.28, rgba("#8b5cf6", 0.34));
      diskGrad.addColorStop(0.55, rgba("#5b21b6", 0.20));
      diskGrad.addColorStop(0.78, rgba("#3b0764", 0.10));
      diskGrad.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = diskGrad;
      ctx.beginPath();
      ctx.ellipse(cx, cy, size * 0.44, size * 0.44, 0, 0, TAU);
      ctx.fill();
      // nebulosas somadas ao disco (feito uma vez: o "screen" lento na CPU não pesa aqui)
      ctx.globalCompositeOperation = "screen";
      this.nebulas.forEach((nebula) => {
        const nx = cx + nebula.x * size * 0.44;
        const ny = cy + nebula.y * size * 0.44;
        const nr = nebula.r * size * 0.44;
        const gradient = ctx.createRadialGradient(nx, ny, 0, nx, ny, nr);
        gradient.addColorStop(0, hsl(nebula.h, 100, 50, nebula.a * 0.88));
        gradient.addColorStop(0.45, hsl(nebula.h, 100, 42, nebula.a * 0.38));
        gradient.addColorStop(1, "rgba(0,0,0,0)");
        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.arc(nx, ny, nr, 0, TAU);
        ctx.fill();
      });
      ctx.restore();
    }

    _ring(atlas, index, size) {
      const ring = this.rings[index];
      const key = `ring:${index}`;
      const found = atlas.get(key);
      if (found) return found;
      const rx = ring.rx * size * 0.5;
      const ry = ring.rx * this.config.diskFlat * size * 0.5;
      const pad = ring.w + 2;
      return atlas.add(key, (rx + pad) * 2, (ry + pad) * 2, (ctx, w, h) => {
        ctx.beginPath();
        ctx.ellipse(w / 2, h / 2, rx, ry, 0, 0, TAU);
        ctx.strokeStyle = ring.c;
        ctx.lineWidth = ring.w;
        ctx.stroke();
      });
    }

    /** Ligação da rede: os dois traços (largo e fraco, fino e forte) num carimbo do tamanho da ligação. */
    _conn(atlas, conn, size) {
      const found = atlas.get(conn.key);
      if (found) return found;
      const a = this.neuralNodes[conn.a];
      const b = this.neuralNodes[conn.b];
      const lineWidth = conn.toCore ? 1.4 : 0.9;
      const pad = lineWidth * 3 + 2;
      const ax = a.x * size * 0.48;
      const ay = a.y * size * 0.48;
      const bx = b.x * size * 0.48;
      const by = b.y * size * 0.48;
      const left = Math.min(ax, bx) - pad;
      const top = Math.min(ay, by) - pad;
      const sprite = atlas.add(conn.key, Math.abs(ax - bx) + pad * 2, Math.abs(ay - by) + pad * 2, (ctx) => {
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.moveTo(ax - left, ay - top);
        ctx.lineTo(bx - left, by - top);
        ctx.strokeStyle = hsl(280, 100, 70, 0.38);
        ctx.lineWidth = lineWidth * 3.0;
        ctx.stroke();
        ctx.globalCompositeOperation = "screen";
        ctx.beginPath();
        ctx.moveTo(ax - left, ay - top);
        ctx.lineTo(bx - left, by - top);
        ctx.strokeStyle = hsl(285, 100, 80, 0.92);
        ctx.lineWidth = lineWidth;
        ctx.stroke();
      });
      if (sprite) sprite.center = { x: left + sprite.w / 2, y: top + sprite.h / 2 };
      return sprite;
    }

    /** Um quadro. time em segundos; frame: {state, level, pointer:{x,y}, reduced}. Devolve o brilho do disco. */
    build(time, frame, atlas, out) {
      out.count = 0;
      if (!this.metrics) return 0;
      const reduced = Boolean(frame.reduced);
      const t = reduced ? time * 0.15 : time;
      const flat = this.config.diskFlat;
      const state = frame.state || "idle";
      const audio = clamp01(frame.level);
      const target = frame.pointer || { x: 0, y: 0 };
      this.pointer.x += (target.x - this.pointer.x) * 0.06;
      this.pointer.y += (target.y - this.pointer.y) * 0.06;
      const { size } = this.metrics;
      const cx = this.metrics.cx + this.pointer.x * size * 0.015;
      const cy = this.metrics.cy + this.pointer.y * size * 0.012;
      this.offset.x = cx - this.metrics.cx;
      this.offset.y = cy - this.metrics.cy;
      const profile = this.profile(state, audio);
      const white = atlas.dot("white", 8);
      const extent = white ? white.extent : 1;

      // estrelas do disco (achatadas junto com o disco)
      const diskAlpha = profile.diskAlpha;
      const diskRotation = -t * this.config.rotationSpeed * profile.rotSpeed * (reduced ? 0.15 : 1);
      for (const star of this.diskStars) {
        const angle = star.a + diskRotation * 0.4 + star.drift * t;
        const px = cx + Math.cos(angle) * star.r * size * 0.42;
        const py = cy + Math.sin(angle) * star.r * size * 0.42 * flat;
        const twinkle = 0.35 + 0.65 * Math.sin(t * star.tws + star.tw);
        const alpha = star.al * twinkle * 0.68 * diskAlpha;
        if (alpha <= 0) continue;
        const glow = atlas.get(star.key) || atlas.glow(star.key, 16, [[0, hsl(star.hue, 100, 90, 1)], [1, "rgba(0,0,0,0)"]]);
        out.push(px, py, star.sz * 2.0, star.sz * 2.0 * flat, 0, glow, alpha);
        const core = star.sz * 0.65 * extent;
        out.push(px, py, core, core * flat, 0, white, Math.min(0.85, alpha * 1.7));
      }

      // estrelas de fora (as grandes com raios em cruz)
      const cross = atlas.get("cross") || atlas.add("cross", 64, 64, (ctx) => {
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 1.1;
        ctx.beginPath();
        ctx.moveTo(0, 32);
        ctx.lineTo(64, 32);
        ctx.moveTo(32, 0);
        ctx.lineTo(32, 64);
        ctx.stroke();
      });
      for (const star of this.outerStars) {
        const angle = star.a + star.drift * t;
        const px = cx + Math.cos(angle) * star.r * size * 0.50;
        const py = cy + Math.sin(angle) * star.r * size * 0.50;
        const twinkle = 0.35 + 0.65 * Math.sin(t * star.tws + star.tw);
        const alpha = star.al * twinkle;
        if (alpha <= 0) continue;
        const glowRadius = star.big ? star.sz * 4.0 : star.med ? star.sz * 2.8 : star.sz * 2.0;
        const glow = atlas.get(star.key) || atlas.glow(star.key, star.big ? 64 : 32, [
          [0, hsl(star.hue, 100, star.big ? 94 : 90, 1)],
          [star.big ? 0.3 : 0.5, hsl(star.hue, 100, 75, star.big ? 0.35 : 0.20)],
          [1, "rgba(0,0,0,0)"],
        ]);
        out.push(px, py, glowRadius, glowRadius, 0, glow, alpha);
        const core = star.sz * (star.big ? 1 : 0.6) * extent;
        out.push(px, py, core, core, 0, white, Math.min(0.95, alpha * (star.big ? 1.1 : 1.5)));
        if (star.big) out.push(px, py, star.sz * 3.0, star.sz * 3.0, 0, cross, alpha * 0.35);
      }

      // braços da espiral
      const armRotation = t * 0.010;
      for (const point of this.armPoints) {
        const frac = (point.r - 0.20) / 0.48;
        const angle = point.angle + armRotation;
        const px = cx + Math.cos(angle) * point.r * size * 0.50;
        const py = cy + Math.sin(angle) * point.r * size * 0.50 * flat;
        const pulse = 0.65 + 0.35 * Math.sin(t * 0.6 + point.phase);
        const alpha = point.al * (0.60 + 0.40 * pulse) * 0.80;
        const glow = atlas.get(point.key) || atlas.glow(point.key, 32, [
          [0, hsl(point.hue, 100, 80, 1)],
          [0.55, hsl(point.hue, 90, 62, 0.22)],
          [1, "rgba(0,0,0,0)"],
        ]);
        const radius = point.sz * (1 + frac * 0.6);
        out.push(px, py, radius, radius, 0, glow, alpha);
      }

      // anéis e os pontos que correm neles
      for (let ringIndex = 0; ringIndex < this.rings.length; ringIndex += 1) {
        const ring = this.rings[ringIndex];
        const rotation = ring.tilt + t * ring.speed;
        const rx = ring.rx * size * 0.5;
        const ry = ring.rx * flat * size * 0.5;
        const pulse = 0.80 + 0.20 * Math.sin(t * 0.5 + ringIndex);
        const sprite = this._ring(atlas, ringIndex, size);
        if (sprite) out.push(cx, cy, sprite.w / 2, sprite.h / 2, rotation, sprite, ring.op * pulse);
        const cos = Math.cos(rotation);
        const sin = Math.sin(rotation);
        for (const dot of this.ringDots) {
          if (dot.ring !== ringIndex) continue;
          const dotAngle = dot.angle + t * ring.speed * 1.3;
          const lx = Math.cos(dotAngle) * rx;
          const ly = Math.sin(dotAngle) * ry;
          const px = cx + lx * cos - ly * sin;
          const py = cy + lx * sin + ly * cos;
          const dotPulse = 0.78 + 0.22 * Math.sin(t * 1.8 + dot.angle);
          const glow = atlas.get(dot.glowKey) || atlas.glow(dot.glowKey, 32, [[0, rgba(dot.c, 1)], [0.4, rgba(dot.c, 0.28)], [1, "rgba(0,0,0,0)"]]);
          out.push(px, py, dot.sz * 2.8, dot.sz * 2.8, 0, glow, dot.al * dotPulse);
          // ponto branco com a sombra borrada já no carimbo (shadowBlur a cada quadro custava caro)
          const core = atlas.get(dot.coreKey) || atlas.dot(dot.coreKey, dot.sz * 0.80, 8, rgba(dot.c, 0.80));
          if (core) out.push(px, py, dot.sz * 0.80 * core.extent, dot.sz * 0.80 * core.extent, 0, core, dot.al * dotPulse);
        }
      }

      // rede neural: ligações (carimbo do tamanho de cada uma), sinais correndo nelas e os nós
      for (const conn of this.neuralConns) {
        const a = this.neuralNodes[conn.a];
        const b = this.neuralNodes[conn.b];
        if (!a || !b) continue;
        const linePulse = 0.38 + 0.62 * Math.sin(t * conn.pulseSpeed + conn.phase);
        const alpha = conn.toCore ? 0.14 + 0.13 * linePulse : 0.08 + 0.10 * linePulse;
        const sprite = this._conn(atlas, conn, size);
        if (sprite) out.push(cx + sprite.center.x, cy + sprite.center.y, sprite.w / 2, sprite.h / 2, 0, sprite, alpha);
        if (!conn.hasSignal) continue;
        const ax = cx + a.x * size * 0.48;
        const ay = cy + a.y * size * 0.48;
        const bx = cx + b.x * size * 0.48;
        const by = cy + b.y * size * 0.48;
        // posição pelo tempo (antes andava por quadro: mais rápido com mais quadros)
        const base = (conn.signalStart + t * conn.signalSpeed) % 1.0;
        const glow = atlas.get(conn.signalKey) || atlas.glow(conn.signalKey, 16, [
          [0, hsl(conn.signalHue, 100, 96, 0.88)],
          [0.4, hsl(conn.signalHue, 100, 82, 0.32)],
          [1, "rgba(0,0,0,0)"],
        ]);
        for (let signalIndex = 0; signalIndex < 2; signalIndex += 1) {
          const pos = (base + (signalIndex ? 0.07 : 0)) % 1.0;
          const sx = ax + (bx - ax) * pos;
          const sy = ay + (by - ay) * pos;
          const signalRadius = signalIndex === 0 ? 1.9 : 1.3;
          out.push(sx, sy, signalRadius * 2.4, signalRadius * 2.4, 0, glow, 1);
          out.push(sx, sy, signalRadius * 0.60 * extent, signalRadius * 0.60 * extent, 0, white, 0.86);
        }
      }
      const speaking = state === "speaking";
      for (const node of this.neuralNodes) {
        const x = cx + node.x * size * 0.48;
        const y = cy + node.y * size * 0.48;
        const pulse = 0.48 + 0.52 * Math.sin(t * node.pulseSpeed + node.phase);
        const nodeAlpha = 0.52 + 0.44 * pulse;
        const nodeRadius = node.s * (0.78 + 0.30 * pulse);
        const halo = atlas.get(node.haloKey) || atlas.glow(node.haloKey, 48, [
          [0, hsl(node.hue, 100, node.isCore ? 90 : 82, node.isCore ? 0.62 : 0.40)],
          [0.4, hsl(node.hue, 100, 68, 0.13)],
          [1, "rgba(0,0,0,0)"],
        ]);
        out.push(x, y, nodeRadius * 4.0, nodeRadius * 4.0, 0, halo, nodeAlpha);
        if (node.isCore) {
          const blur = Math.round(speaking ? 18 : 28 * profile.glowMult);
          const core = atlas.dot(`nodecore:${node.hue}:${blur}`, node.s * 1.08 * 0.70, blur, hsl(node.hue, 100, 88, 0.88));
          if (core) out.push(x, y, nodeRadius * 0.70 * core.extent, nodeRadius * 0.70 * core.extent, 0, core, nodeAlpha * 0.88);
        } else {
          out.push(x, y, nodeRadius * 0.70 * extent, nodeRadius * 0.70 * extent, 0, white, nodeAlpha * 0.88);
        }
      }

      // núcleo: halos, pulso da voz, centro e o clarão
      const pulseAmp = speaking ? 0.06 + audio * 0.08 : 0.022;
      const nucleusRadius = size * this.config.nucleusSize * (1 + Math.sin(t * profile.pulseFreq) * pulseAmp);
      const halo = atlas.glow(`halo:${profile.coreGlow}:${profile.aura}`, 192, [
        [0, rgba(profile.coreGlow, 1)],
        [0.45, rgba(profile.aura, 0.30)],
        [1, "rgba(0,0,0,0)"],
      ]);
      const layers = speaking
        ? [2.2 + audio * 1.5, 0.10 + audio * 0.22, 4.0 + audio * 2.0, 0.06 + audio * 0.14, 6.5 + audio * 3.0, 0.03 + audio * 0.08]
        : [2.6, 0.08, 4.0, 0.04, 6.5, 0.02];
      for (let i = 0; i < 6; i += 2) {
        const radius = nucleusRadius * layers[i];
        out.push(cx, cy, radius, radius, 0, halo, Math.min(0.80, layers[i + 1] * profile.glowMult));
      }
      if (speaking && audio > 0.06) {
        const voice = atlas.glow(`voicepulse:${profile.coreGlow}`, 192, [
          [0, rgba(profile.coreGlow, 1)],
          [0.5, rgba(profile.coreGlow, 0.27)],
          [1, "rgba(0,0,0,0)"],
        ], 0.3);
        const radius = nucleusRadius * (2.2 + audio * 2.5);
        out.push(cx, cy, radius, radius, 0, voice, audio * 0.30);
      }
      const core = atlas.glow(`core:${profile.coreInner}:${profile.coreMid}:${profile.coreGlow}`, 192, [
        [0, rgba("#ffffff", 0.92)],
        [0.08, rgba(profile.coreInner, 0.85)],
        [0.22, rgba(profile.coreMid, 0.70)],
        [0.42, rgba(profile.coreGlow, 0.50)],
        [0.60, rgba("#6d28d9", 0.28)],
        [0.78, rgba("#4c1d95", 0.12)],
        [1, "rgba(0,0,0,0)"],
      ]);
      out.push(cx, cy, nucleusRadius * 2.8, nucleusRadius * 2.8, 0, core, 1);
      const flare = atlas.glow("flare", 64, [[0, rgba("#ffffff", 1.0)], [0.4, rgba("#f5d0fe", 0.80)], [1, "rgba(0,0,0,0)"]]);
      out.push(cx, cy, nucleusRadius * 0.55, nucleusRadius * 0.55, 0, flare, 1);
      return diskAlpha;
    }
  }

  const VERTEX_SHADER = `#version 300 es
in vec2 corner;
in vec4 place;
in vec2 turnAlpha;
in vec4 rect;
uniform vec2 viewport;
out vec2 uv;
out float alpha;
void main() {
  float c = cos(turnAlpha.x);
  float s = sin(turnAlpha.x);
  vec2 local = corner * place.zw;
  vec2 pos = place.xy + vec2(local.x * c - local.y * s, local.x * s + local.y * c);
  vec2 clip = pos / viewport * 2.0 - 1.0;
  gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
  uv = mix(rect.xy, rect.zw, corner * 0.5 + 0.5);
  alpha = turnAlpha.y;
}`;
  const FRAGMENT_SHADER = `#version 300 es
precision mediump float;
in vec2 uv;
in float alpha;
uniform sampler2D atlas;
out vec4 color;
void main() {
  color = texture(atlas, uv) * alpha;
}`;

  /** Desenho pela placa: atlas e disco como texturas, todos os carimbos num comando só (instanciado). */
  class WebGLBackend {
    constructor(canvas) {
      // desynchronized: sem ele, a entrega do quadro travava a página ~110 ms uma vez por segundo (medido na Kira em
      // 2026-10-25: 7 quadros longos em 8 s; com ele, 1). O painel continua por cima do orbe (conferido com o clima).
      const gl = canvas.getContext("webgl2", { alpha: true, premultipliedAlpha: true, antialias: false, depth: false, stencil: false, preserveDrawingBuffer: false, desynchronized: true });
      if (!gl) throw new Error("WebGL2 indisponivel");
      this.kind = "webgl";
      this.gl = gl;
      this.canvas = canvas;
      const compile = (type, source) => {
        const shader = gl.createShader(type);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader) || "shader");
        return shader;
      };
      const program = gl.createProgram();
      gl.attachShader(program, compile(gl.VERTEX_SHADER, VERTEX_SHADER));
      gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAGMENT_SHADER));
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program) || "programa");
      this.program = program;
      this.viewport = gl.getUniformLocation(program, "viewport");
      gl.useProgram(program);
      gl.uniform1i(gl.getUniformLocation(program, "atlas"), 0);

      this.vao = gl.createVertexArray();
      gl.bindVertexArray(this.vao);
      const corners = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, corners);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      const cornerLoc = gl.getAttribLocation(program, "corner");
      gl.enableVertexAttribArray(cornerLoc);
      gl.vertexAttribPointer(cornerLoc, 2, gl.FLOAT, false, 0, 0);
      this.instanceBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, this.instanceBuffer);
      const bytes = STRIDE * 4;
      [["place", 4, 0], ["turnAlpha", 2, 16], ["rect", 4, 24]].forEach(([name, count, offset]) => {
        const loc = gl.getAttribLocation(program, name);
        gl.enableVertexAttribArray(loc);
        gl.vertexAttribPointer(loc, count, gl.FLOAT, false, bytes, offset);
        gl.vertexAttribDivisor(loc, 1);
      });
      gl.bindVertexArray(null);
      this.diskInstance = new Float32Array(STRIDE);

      const texture = () => {
        const tex = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, tex);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        return tex;
      };
      this.atlasTexture = texture();
      this.diskTexture = texture();
      this.atlasVersion = -1;
      this.diskVersion = -1;
    }

    _upload(textureId, source) {
      const gl = this.gl;
      gl.bindTexture(gl.TEXTURE_2D, textureId);
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
    }

    resize(width, height, dpr) {
      this.canvas.width = Math.round(width * dpr);
      this.canvas.height = Math.round(height * dpr);
    }

    lost() {
      return this.gl.isContextLost();
    }

    draw(scene, atlas, disk, out, diskAlpha) {
      const gl = this.gl;
      const { width, height } = scene.bounds;
      gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.useProgram(this.program);
      gl.uniform2f(this.viewport, width, height);
      gl.bindVertexArray(this.vao);
      gl.enable(gl.BLEND);
      gl.activeTexture(gl.TEXTURE0);
      // disco por baixo (por cima do vazio: "source-over")
      if (disk.version !== this.diskVersion) {
        this._upload(this.diskTexture, disk.canvas);
        this.diskVersion = disk.version;
      }
      const d = this.diskInstance;
      d[0] = width / 2 + scene.offset.x;
      d[1] = height / 2 + scene.offset.y;
      d[2] = width / 2;
      d[3] = height / 2;
      d[4] = 0;
      d[5] = clamp01(diskAlpha);
      d[6] = 0;
      d[7] = 0;
      d[8] = 1;
      d[9] = 1;
      gl.bindTexture(gl.TEXTURE_2D, this.diskTexture);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.instanceBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, d, gl.STREAM_DRAW);
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, 1);
      // todos os carimbos somando em "screen": cor = origem + destino × (1 − origem)
      if (atlas.version !== this.atlasVersion) {
        this._upload(this.atlasTexture, atlas.canvas);
        this.atlasVersion = atlas.version;
      }
      gl.bindTexture(gl.TEXTURE_2D, this.atlasTexture);
      gl.bufferData(gl.ARRAY_BUFFER, out.data.subarray(0, out.count * STRIDE), gl.STREAM_DRAW);
      gl.blendFuncSeparate(gl.ONE, gl.ONE_MINUS_SRC_COLOR, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, out.count);
      gl.bindVertexArray(null);
    }
  }

  /** Reserva sem WebGL: o mesmo atlas desenhado por um canvas 2D, um carimbo por vez. */
  class Canvas2DBackend {
    constructor(canvas) {
      this.kind = "canvas2d";
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d", { alpha: true });
      if (!this.ctx) throw new Error("Canvas 2D indisponivel.");
      this.dpr = 1;
    }

    resize(width, height, dpr) {
      this.canvas.width = Math.round(width * dpr);
      this.canvas.height = Math.round(height * dpr);
      this.dpr = dpr;
    }

    lost() {
      return false;
    }

    draw(scene, atlas, disk, out, diskAlpha) {
      const ctx = this.ctx;
      const dpr = this.dpr;
      const { width, height } = scene.bounds;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.globalCompositeOperation = "source-over";
      ctx.globalAlpha = 1;
      ctx.clearRect(0, 0, width, height);
      ctx.globalAlpha = clamp01(diskAlpha);
      ctx.drawImage(disk.canvas, scene.offset.x, scene.offset.y, width, height);
      ctx.globalCompositeOperation = "screen";
      const d = out.data;
      const size = atlas.canvas.width;
      for (let i = 0; i < out.count; i += 1) {
        const o = i * STRIDE;
        const sx = d[o + 6] * size;
        const sy = d[o + 7] * size;
        const sw = (d[o + 8] - d[o + 6]) * size;
        const sh = (d[o + 9] - d[o + 7]) * size;
        ctx.globalAlpha = d[o + 5];
        if (d[o + 4]) {
          ctx.translate(d[o], d[o + 1]);
          ctx.rotate(d[o + 4]);
          ctx.drawImage(atlas.canvas, sx, sy, sw, sh, -d[o + 2], -d[o + 3], d[o + 2] * 2, d[o + 3] * 2);
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        } else {
          ctx.drawImage(atlas.canvas, sx, sy, sw, sh, d[o] - d[o + 2], d[o + 1] - d[o + 3], d[o + 2] * 2, d[o + 3] * 2);
        }
      }
      ctx.globalCompositeOperation = "source-over";
      ctx.globalAlpha = 1;
    }
  }

  /**
   * Laço de quadros do orbe: 30 por segundo em todos os estados (antes eram 10 parado, e parecia travado). Mais que
   * isso não compensa nesta máquina: cada quadro novo da tela custa ~1% de CPU por quadro/segundo só para chegar à
   * janela (Chromium -> Qt, medido em 2026-10-25 com um quadradinho de 8 px mudando), e 60 por segundo disputariam a
   * CPU com o áudio e a conversa. readLevel: nível da voz da página.
   */
  class KiraOrbLoop {
    constructor(canvas, { raf, caf, config, readLevel } = {}) {
      this.scene = new OrbScene(config);
      this.atlas = new Atlas();
      this.disk = { canvas: makeCanvas(1, 1), version: 0 };
      this.out = new Instances();
      try {
        this.backend = new WebGLBackend(canvas);
      } catch (error) {
        console.warn("[KIRA ORB] sem WebGL, desenhando em canvas 2D:", error?.message || error);
        this.backend = new Canvas2DBackend(canvas);
      }
      this.raf = raf;
      this.caf = caf;
      this.readLevel = readLevel || (() => 0);
      this.state = "idle";
      this.levelBoost = 0;
      this.level = 0;
      this.pointer = { x: 0, y: 0 };
      this.reduced = false;
      this.running = false;
      this.frameId = 0;
      this.lastAt = 0;
      this.tick = this.tick.bind(this);
    }

    get kind() {
      return this.backend.kind;
    }

    /** Tamanho em pixels da tela (CSS) e densidade; false enquanto o orbe não tem tamanho. */
    resize(width, height, dpr) {
      if (!(width >= 32 && height >= 32)) {
        this.scene.metrics = null;
        return false;
      }
      const ratio = Math.min(Number(dpr) || 1, 2);
      const before = this.scene.bounds;
      const changed = !before || before.width !== width || before.height !== height || before.dpr !== ratio;
      this.scene.bounds = { width, height, dpr: ratio };
      this.scene.metrics = { size: Math.min(width, height), cx: width * 0.5, cy: height * 0.5 };
      if (changed) {
        this.backend.resize(width, height, ratio);
        this._repaintShapes();
      }
      return true;
    }

    /**
     * Anéis, ligações e disco dependem do tamanho e do achatamento: refaz o atlas e o disco. Já cria os carimbos de
     * todos os estados: criados na hora da troca (ex.: a Kira começa a falar), o atlas inteiro subia de novo para a
     * placa no meio da conversa (~70 ms parada, medido em 2026-10-25).
     */
    _repaintShapes() {
      const bounds = this.scene.bounds;
      if (!bounds) return;
      this.atlas.reset();
      const disk = makeCanvas(Math.round(bounds.width * bounds.dpr), Math.round(bounds.height * bounds.dpr));
      this.scene.paintDisk(disk, bounds.width, bounds.height, bounds.dpr);
      this.disk = { canvas: disk, version: this.disk.version + 1 };
      const pointer = { ...this.scene.pointer };
      const scratch = new Instances();
      Object.keys(PROFILES).forEach((state) => this.scene.build(0, { state, level: 0.5 }, this.atlas, scratch));
      this.scene.pointer = pointer;
    }

    configure(config) {
      const flatBefore = this.scene.config.diskFlat;
      this.scene.configure(config);
      if (this.scene.config.diskFlat !== flatBefore) this._repaintShapes();
    }

    setState(state) {
      this.state = state || "idle";
    }

    boost(value) {
      this.level = clamp01(value);
      this.levelBoost = this.level;
    }

    setPointer(x, y) {
      this.pointer = { x: Number(x) || 0, y: Number(y) || 0 };
    }

    setReduced(on) {
      this.reduced = Boolean(on);
    }

    run(on) {
      if (on && !this.running) {
        this.running = true;
        this.lastAt = 0;
        this.frameId = this.raf(this.tick);
      } else if (!on && this.running) {
        this.running = false;
        if (this.frameId) this.caf(this.frameId);
        this.frameId = 0;
      }
    }

    tick(timestamp) {
      this.frameId = 0;
      if (!this.running) return;
      if (!this.lastAt || timestamp - this.lastAt >= frameMsNow() - 2) {
        this.lastAt = timestamp;
        const speaking = this.state === "speaking";
        const input = speaking ? clamp01(this.readLevel()) : 0;
        const target = speaking ? Math.max(input, this.levelBoost) : input;
        this.level += (target - this.level) * (target > this.level ? 0.35 : 0.08);
        this.levelBoost *= 0.92;
        if (this.backend.lost()) {
          this.running = false;  // a placa perdeu o contexto: o orbe para (a página continua)
          return;
        }
        const diskAlpha = this.scene.build(timestamp * 0.001, { state: this.state, level: this.level, pointer: this.pointer, reduced: this.reduced }, this.atlas, this.out);
        if (this.scene.metrics) this.backend.draw(this.scene, this.atlas, this.disk, this.out, diskAlpha);
      }
      this.frameId = this.raf(this.tick);
    }
  }

  root.KiraOrbLoop = KiraOrbLoop;
  root.KIRA_ORB_DEFAULT_CONFIG = DEFAULT_CONFIG;
})(typeof self !== "undefined" ? self : window);
