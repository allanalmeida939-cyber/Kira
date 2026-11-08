// Orbes Constelação, Colmeia e Buraco negro (aba Aparência). Mesmo contrato do orbRenderers.js:
// draw(ctx, { t, w, h, state, level, accent }). Cada um suaviza a troca de estado sozinho (pesos idle/ouvindo/falando/trabalhando).
// O buraco negro é um shader WebGL desenhado num canvas próprio, em resolução reduzida, e copiado para o ctx.

const TAU = Math.PI * 2;
const lerp = (a, b, k) => a + (b - a) * k;
const clamp = (v, a = 0, b = 1) => Math.max(a, Math.min(b, v));
const wave = (t, k) => Math.sin(t * 1.7 + k) * 0.5 + Math.sin(t * 3.1 + k * 2.3) * 0.3 + Math.sin(t * 5.3 + k * 0.7) * 0.2;
const rotY = (p, a) => { const c = Math.cos(a), s = Math.sin(a); return [p[0] * c + p[2] * s, p[1], -p[0] * s + p[2] * c]; };
const rotX = (p, a) => { const c = Math.cos(a), s = Math.sin(a); return [p[0], p[1] * c - p[2] * s, p[1] * s + p[2] * c]; };
// câmera do arraste (orbSpin.js): girar o orbe com o mouse mostra frente, trás, topo e lados
const VY = () => globalThis.kiraOrbView?.yaw || 0;
const VX = () => globalThis.kiraOrbView?.pitch || 0;
const rotZ = (p, a) => { const c = Math.cos(a), s = Math.sin(a); return [p[0] * c - p[1] * s, p[0] * s + p[1] * c, p[2]]; };
const rgba = ([r, g, b], a) => `rgba(${r | 0},${g | 0},${b | 0},${clamp(a).toFixed(3)})`;
const mix = (c1, c2, k) => [lerp(c1[0], c2[0], k), lerp(c1[1], c2[1], k), lerp(c1[2], c2[2], k)];
const CYAN = [103, 232, 249];
const PINK = [244, 114, 182];
const WHITE = [255, 255, 255];

function fib(n) {
  const out = [];
  for (let i = 0; i < n; i += 1) {
    const y = 1 - (2 * (i + 0.5)) / n;
    const r = Math.sqrt(1 - y * y);
    const a = i * 2.39996;
    out.push([Math.cos(a) * r, y, Math.sin(a) * r]);
  }
  return out;
}

function glow(ctx, x, y, r, color, alpha) {
  if (r <= 0 || alpha <= 0) return;
  const g = ctx.createRadialGradient(x, y, 0, x, y, r);
  g.addColorStop(0, rgba(color, alpha));
  g.addColorStop(1, rgba(color, 0));
  ctx.fillStyle = g;
  ctx.beginPath();
  ctx.arc(x, y, r, 0, TAU);
  ctx.fill();
}

function dot(ctx, x, y, r) {
  ctx.beginPath();
  ctx.arc(x, y, r, 0, TAU);
  ctx.fill();
}

// Pesos dos estados: sobem/descem suave para a troca não dar salto. "error" desenha como parado.
function createMood() {
  const S = { idle: 1, ouv: 0, fal: 0, trab: 0 };
  let last = 0;
  let prevLevel = 0;
  return {
    S,
    update(t, state, level) {
      const dt = last ? clamp(t - last, 0, 0.1) : 0.016;
      last = t;
      const target = state === 'listening' ? 'ouv' : state === 'speaking' ? 'fal' : state === 'thinking' ? 'trab' : 'idle';
      const k = 1 - Math.pow(0.004, dt);
      for (const key of Object.keys(S)) S[key] = lerp(S[key], key === target ? 1 : 0, k);
      // Sílaba: o nível da voz cruzando para cima.
      const syllable = level > 0.5 && prevLevel <= 0.5;
      prevLevel = level;
      return { dt, syllable };
    },
  };
}

/** "Constelação": estrelas ligadas às duas vizinhas. Ouvindo puxa para dentro; falando explode em rosa a cada sílaba. */
export function createConstellationRenderer() {
  const mood = createMood();
  const pts = fib(96).map((v) => {
    const j = v.map((c) => c + (Math.random() - 0.5) * 0.12);
    const m = Math.hypot(...j);
    return j.map((c) => c / m);
  });
  const spread = pts.map(() => 0.5 + Math.random());
  const twinkle = pts.map(() => Math.random() * TAU);
  const edges = [];
  const seen = new Set();
  pts.forEach((a, i) => {
    pts.map((b, j) => [j, Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])])
      .filter(([j]) => j !== i).sort((u, v) => u[1] - v[1]).slice(0, 2)
      .forEach(([j]) => { const key = i < j ? `${i}-${j}` : `${j}-${i}`; if (!seen.has(key)) { seen.add(key); edges.push([i, j]); } });
  });
  let spin = 0;
  let shocks = [];
  return {
    draw(ctx, { t, w, h, state, level, accent }) {
      const { dt, syllable } = mood.update(t, state, level);
      const { S } = mood;
      const main = accent.main;
      const R = Math.min(w, h) * 0.44;
      const lv = level;
      spin += dt * (0.18 + S.trab * 0.7 + S.fal * lv * 0.6);
      if (S.fal > 0.3 && syllable) shocks.push(t);
      shocks = shocks.filter((s) => t - s < 1.2);
      ctx.clearRect(0, 0, w, h);
      ctx.save();
      ctx.translate(w / 2, h / 2);
      ctx.globalCompositeOperation = 'lighter';
      const r = R * 0.56;
      const hot = S.fal * lv;
      // Aura de fala: halo grande que acompanha a voz, mais raios girando.
      glow(ctx, 0, 0, r * (1.3 + hot * 0.7), mix(main, PINK, hot), 0.22 + hot * 0.4);
      if (S.fal > 0.05) {
        for (let k = 0; k < 12; k += 1) {
          const a = k / 12 * TAU + t * 0.6;
          const len = r * (1.15 + lv * 0.6 * (0.5 + 0.5 * Math.sin(t * 7 + k)));
          const g = ctx.createLinearGradient(0, 0, Math.cos(a) * len, Math.sin(a) * len);
          g.addColorStop(0, rgba(PINK, 0));
          g.addColorStop(0.6, rgba(PINK, 0.18 * S.fal * lv));
          g.addColorStop(1, rgba(PINK, 0));
          ctx.strokeStyle = g;
          ctx.lineWidth = 2 + lv * 3;
          ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(Math.cos(a) * len, Math.sin(a) * len); ctx.stroke();
        }
      }
      const pull = (d) => {
        let m = 0;
        for (let i = 0; i < 3; i += 1) {
          const ph = (t * 0.7 + i / 3) % 1;
          m = Math.max(m, Math.exp(-Math.pow((d - (1.7 - ph * 1.7)) * 7, 2)));
        }
        return m;
      };
      if (S.ouv > 0.05) {
        for (let i = 0; i < 3; i += 1) {
          const ph = (t * 0.7 + i / 3) % 1;
          ctx.strokeStyle = rgba(accent.listen, S.ouv * ph * 0.55);
          ctx.lineWidth = 1 + ph * 1.5;
          ctx.beginPath(); ctx.arc(0, 0, r * (1.7 - ph * 1.7) + 2, 0, TAU); ctx.stroke();
        }
      }
      for (const s of shocks) {
        const k = (t - s) / 1.2;
        ctx.strokeStyle = rgba(PINK, S.fal * (1 - k) * 0.75);
        ctx.lineWidth = 3 * (1 - k) + 0.5;
        ctx.beginPath(); ctx.arc(0, 0, r * (1 + k * 0.75), 0, TAU); ctx.stroke();
      }
      const q = pts.map((b, i) => {
        const v = rotX(rotY(b, spin + VY()), 0.32 + VX());
        const p = S.ouv * pull(Math.hypot(v[0], v[1])) * 0.28 * (0.6 + 0.4);
        const jit = S.ouv * p * Math.sin(t * 40 + i) * 0.04;
        const blast = hot * 0.55 * spread[i];
        const k = 1 - p + jit + blast;
        return [v[0] * r * k, v[1] * r * k, v[2], p, blast];
      });
      ctx.lineWidth = 1;
      edges.forEach(([i, j], n) => {
        const a = q[i];
        const b = q[j];
        const z = (a[2] + b[2]) / 2;
        const spark = Math.max(a[3], b[3]) * 3;
        ctx.strokeStyle = spark > 0.3 ? rgba([240, 220, 255], 0.3 + spark) : rgba(mix(accent.soft, PINK, hot), (0.1 + (z + 1) * 0.16));
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
        if (S.trab > 0.05) {
          const ph = (t * 1.4 + n * 0.37) % 1;
          ctx.fillStyle = rgba(CYAN, 0.9 * S.trab);
          dot(ctx, lerp(a[0], b[0], ph), lerp(a[1], b[1], ph), 2.2);
        }
        if (hot > 0.15) {
          const ph = (t * 2.2 + n * 0.21) % 1;
          ctx.fillStyle = rgba([255, 220, 250], hot);
          dot(ctx, lerp(a[0], b[0], ph), lerp(a[1], b[1], ph), 1.4 + hot * 1.6);
        }
      });
      const scan = Math.sin(t * 1.3) * r;
      q.forEach((v, i) => {
        const tw = 0.55 + 0.45 * Math.sin(t * 2.2 + twinkle[i]);
        const hit = S.trab * clamp(1 - Math.abs(v[1] - scan) / (r * 0.12));
        const size = (1.2 + (v[2] + 1) * 1.1) * (1 + v[4] * 2.2 + v[3] * 2) + hit * 2;
        ctx.fillStyle = v[4] > 0.15
          ? rgba([255, 200 - v[4] * 120, 240], 0.5 + v[4])
          : rgba(mix([245, 225, 255], CYAN, hit), (0.35 + (v[2] + 1) * 0.3) * tw + hit * 0.4 + v[3]);
        dot(ctx, v[0], v[1], size);
        if (v[2] > 0.3 || v[4] > 0.3) {
          ctx.globalAlpha = 0.22;
          dot(ctx, v[0], v[1], size * 3.2);
          ctx.globalAlpha = 1;
        }
      });
      if (S.trab > 0.05) {
        ctx.strokeStyle = rgba(CYAN, 0.5 * S.trab);
        const ww = Math.sqrt(Math.max(0, r * r - scan * scan));
        ctx.beginPath(); ctx.ellipse(0, scan, ww, ww * 0.16, 0, 0, TAU); ctx.stroke();
        ctx.strokeStyle = rgba(CYAN, 0.35 * S.trab);
        ctx.beginPath(); ctx.ellipse(0, 0, R * 0.86, R * 0.24, -0.35, 0, TAU); ctx.stroke();
        const a = t * 1.8;
        const p = rotZ([Math.cos(a) * R * 0.86, Math.sin(a) * R * 0.24, 0], -0.35);
        ctx.fillStyle = rgba([207, 250, 254], S.trab);
        dot(ctx, p[0], p[1], 3);
        glow(ctx, p[0], p[1], 12, CYAN, S.trab);
      }
      glow(ctx, 0, 0, r * (0.2 + hot * 0.6 + S.ouv * 0.05 * Math.sin(t * 12)), hot > 0.3 ? [255, 230, 250] : [255, 250, 255], 0.95);
      ctx.restore();
      return { core: r * 0.2 };
    },
  };
}

/** "Colmeia": esfera de hexágonos. Falando as células saltam como equalizador; trabalhando as faixas giram como um cofre. */
export function createHiveRenderer() {
  const mood = createMood();
  const pts = fib(150);
  const seed = pts.map(() => Math.random());
  const band = pts.map((n) => Math.min(4, Math.floor(((n[1] + 1) / 2) * 5)));
  const hex = pts.map((n) => {
    const up = Math.abs(n[1]) > 0.9 ? [1, 0, 0] : [0, 1, 0];
    let t1 = [up[1] * n[2] - up[2] * n[1], up[2] * n[0] - up[0] * n[2], up[0] * n[1] - up[1] * n[0]];
    const m = Math.hypot(...t1);
    t1 = t1.map((c) => c / m);
    const t2 = [n[1] * t1[2] - n[2] * t1[1], n[2] * t1[0] - n[0] * t1[2], n[0] * t1[1] - n[1] * t1[0]];
    return Array.from({ length: 6 }, (_, k) => {
      const a = (k / 6) * TAU;
      return [0, 1, 2].map((c) => t1[c] * Math.cos(a) * 0.13 + t2[c] * Math.sin(a) * 0.13);
    });
  });
  let spin = 0;
  return {
    draw(ctx, { t, w, h, state, level, accent }) {
      const { dt } = mood.update(t, state, level);
      const { S } = mood;
      const lv = level;
      const hot = S.fal * lv;
      const R = Math.min(w, h) * 0.44;
      spin += dt * (0.24 + S.trab * 0.12);
      const r = R * 0.52 * (1 + hot * 0.08);
      ctx.clearRect(0, 0, w, h);
      ctx.save();
      ctx.translate(w / 2, h / 2);
      ctx.globalCompositeOperation = 'lighter';
      glow(ctx, 0, 0, r * (1.3 + hot * 0.6), mix(accent.main, [219, 39, 119], hot), 0.42 + hot * 0.3);
      // Falando: anéis de luz saindo da esfera em cada pulso de voz.
      if (S.fal > 0.05) {
        for (let i = 0; i < 3; i += 1) {
          const ph = (t * 1.1 + i / 3) % 1;
          ctx.strokeStyle = rgba(mix(accent.soft, PINK, 0.5), S.fal * (1 - ph) * (0.25 + lv * 0.5));
          ctx.lineWidth = 2 * (1 - ph) + 0.5;
          ctx.beginPath(); ctx.arc(0, 0, r * (1.05 + ph * 0.7), 0, TAU); ctx.stroke();
        }
      }
      ctx.globalCompositeOperation = 'source-over';
      const bandSpin = (b) => S.trab * t * (b % 2 ? 1.4 : -1.4) * (0.6 + b * 0.15);
      const vy = VY(); const vx = VX();
      const tf = (v, i) => rotX(rotY(v, spin + bandSpin(band[i]) + vy), 0.3 + vx);
      // rotação é linear: gira o centro e os 6 cantos uma vez por quadro e só soma (antes eram ~13 rotações por célula)
      const cells = [];
      for (let i = 0; i < pts.length; i += 1) {
        const c = tf(pts[i], i);
        if (c[2] < -0.05) continue;
        cells.push({ i, c, offs: hex[i].map((o) => tf(o, i)) });
      }
      cells.sort((a, b) => a.c[2] - b.c[2]);
      for (const { i, c, offs } of cells) {
        const n = pts[i];
        const ang = Math.acos(clamp(c[2], -1, 1));
        const ripple = S.ouv * Math.pow(clamp(0.5 + 0.5 * Math.sin(ang * 7 + t * 6)), 6);
        const amp = clamp(0.5 + 0.5 * wave(t * 4.5, Math.floor((n[1] + 1) * 6) * 1.7 + n[0] * 2));
        const ext = 1 + hot * 0.7 * amp * amp;
        const data = S.trab * (Math.sin(t * 9 + seed[i] * 60) > 0.6 ? 1 : 0);
        const top = offs.map((o) => [(c[0] + o[0]) * ext, (c[1] + o[1]) * ext]);
        if (ext > 1.03) {
          const base = offs.map((o) => [c[0] + o[0], c[1] + o[1]]);
          for (let k = 0; k < 6; k += 1) {
            const k2 = (k + 1) % 6;
            ctx.beginPath();
            ctx.moveTo(base[k][0] * r, base[k][1] * r);
            ctx.lineTo(base[k2][0] * r, base[k2][1] * r);
            ctx.lineTo(top[k2][0] * r, top[k2][1] * r);
            ctx.lineTo(top[k][0] * r, top[k][1] * r);
            ctx.closePath();
            ctx.fillStyle = `hsla(${300 - amp * 40},70%,${10 + k * 4}%,0.85)`;
            ctx.fill();
          }
        }
        ctx.beginPath();
        top.forEach((v, j) => (j ? ctx.lineTo(v[0] * r, v[1] * r) : ctx.moveTo(v[0] * r, v[1] * r)));
        ctx.closePath();
        const lift = ext - 1;
        ctx.fillStyle = data
          ? `hsla(186,95%,${55 + seed[i] * 25}%,0.95)`
          : lift > 0.05
            ? `hsla(${300 - lift * 130},90%,${45 + lift * 55}%,0.95)`
            : `hsla(${282 - ripple * 40},80%,${14 + c[2] * 26 + ripple * 50}%,0.92)`;
        ctx.fill();
        ctx.strokeStyle = data ? 'rgba(207,250,254,.9)' : `rgba(233,213,255,${clamp(0.12 + c[2] * 0.45 + ripple * 0.4 + lift)})`;
        ctx.lineWidth = 1;
        ctx.stroke();
      }
      ctx.globalCompositeOperation = 'lighter';
      if (S.trab > 0.05) {
        const pr = (t * 0.25) % 1;
        ctx.strokeStyle = rgba(CYAN, 0.25 * S.trab);
        ctx.lineWidth = 3;
        ctx.beginPath(); ctx.arc(0, 0, R * 0.86, 0, TAU); ctx.stroke();
        ctx.strokeStyle = rgba(CYAN, S.trab);
        ctx.beginPath(); ctx.arc(0, 0, R * 0.86, -Math.PI / 2, -Math.PI / 2 + pr * TAU); ctx.stroke();
      }
      glow(ctx, 0, 0, R * (0.3 + hot * 0.25), [232, 121, 249], 0.1 + hot * 0.45);
      ctx.restore();
      return { core: r };
    },
  };
}

const BLACK_HOLE_FS = `precision mediump float;uniform vec2 res;uniform float t,ouv,fal,trab,lv,sp;uniform vec3 ca,cb;
float h(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
float n(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);return mix(mix(h(i),h(i+vec2(1,0)),f.x),mix(h(i+vec2(0,1)),h(i+vec2(1,1)),f.x),f.y);}
float fbm(vec2 p){float v=0.,a=.5;for(int i=0;i<5;i++){v+=a*n(p);p=p*2.03+vec2(1.7,9.2);a*=.5;}return v;}
vec3 pal(float k){return mix(mix(ca*.6,cb,k),vec3(1.,.94,1.),smoothstep(.75,1.,k));}
void main(){vec2 uv=(gl_FragCoord.xy-.5*res)/res.y*2.3;float hot=fal*lv;
 float rs=.34;float d=length(uv);float ang=atan(uv.y,uv.x);float tilt=.3+trab*.12*sin(t*.9);
 vec2 suv=uv*(1.+rs*rs*1.6/(d*d+.02));vec3 col=vec3(0.);
 col+=vec3(.9,.85,1.)*pow(h(floor(suv*90.)),400.)*3.*smoothstep(.2,.6,d)*smoothstep(1.12,.85,d);
 col+=ca*.35*fbm(suv*1.6+vec2(t*.01,0.))*smoothstep(1.1,.3,d);
 vec2 p=vec2(uv.x,uv.y/tilt);float r=length(p);float a=atan(p.y,p.x);
 float inner=rs*1.18,outer=.95-ouv*.12+hot*.22;
 float band=smoothstep(inner,inner+.06,r)*smoothstep(outer,outer-.35,r);
 float inflow=t*(.25+ouv*1.6+trab*.6);float sw=a*2.+sp*1.2/(r+.15)+ouv*(r*9.);
 float tex=fbm(vec2(r*7.+inflow,sw));float tex2=fbm(vec2(r*18.+inflow*2.,sw*2.5));
 float dop=.55+.45*cos(a+.3);
 float bri=(.35+tex*1.1+tex2*.5)*dop*(1.+hot*3.2)*(1.-ouv*.25)*smoothstep(1.2,.3,r);
 vec3 dc=pal(clamp(tex*.9+dop*.25+hot*.35,0.,1.));dc=mix(dc,vec3(.25,.9,1.),trab*.55*tex2);
 vec3 disk=dc*bri*band*1.7;
 float lr=d/rs;float arc=smoothstep(1.03,1.1,lr)*smoothstep(1.85+hot*.5,1.15,lr);
 float atex=fbm(vec2(d*16.-inflow*1.5,ang*3.+sp*.9));float up=mix(.35,1.,smoothstep(-.3,.6,uv.y/max(d,.001)));
 vec3 arcC=pal(clamp(atex+.15,0.,1.))*arc*(.4+atex)*up*(1.+hot*2.6)*(.75+.25*cos(ang+.3));arcC=mix(arcC,arcC.zyx*vec3(.4,1.,1.2),trab*.4);
 col+=arcC;if(uv.y>0.)col+=disk*.85;float sh=smoothstep(rs*1.01,rs*.97,d);col*=1.-sh;if(uv.y<=0.)col+=disk*(1.-sh*.15);
 col+=vec3(1.,.85,1.)*exp(-abs(d-rs*1.02)*(90.-ouv*40.))*(.9+ouv*lv*2.5*(.5+.5*sin(t*14.))+hot*1.6);
 col+=cb*hot*.5*exp(-abs(d-rs*1.25)*14.);
 for(int i=0;i<3;i++){float ph=fract(t*.6+float(i)/3.);col+=vec3(.6,.5,1.)*ouv*ph*.35*exp(-abs(d-(rs+(1.-ph)))*60.);}
 for(int i=0;i<2;i++){float sk=fract(t*1.6+float(i)*.5);col+=mix(cb,vec3(1.),.4)*hot*(1.-sk)*.9*exp(-abs(d-(rs+sk*1.1))*45.);}
 float jl=hot*1.2+trab*.25;float jet=exp(-abs(uv.x)*(55.-abs(uv.y)*25.))*smoothstep(rs*.8,rs*1.3,abs(uv.y))*smoothstep(rs+jl+.05,rs,abs(uv.y));
 jet*=.6+.4*n(vec2(uv.y*12.-t*8.,0.));col+=mix(vec3(1.,.6,1.),vec3(.4,.95,1.),trab)*jet*1.8;
 float tk=step(.82,fract(ang/6.2832*72.))*exp(-abs(d-1.02)*120.);float aa=fract((ang+1.5708)/6.2832);
 col+=vec3(.4,.95,1.)*trab*tk*(aa<fract(t*.18)?1.:.25);
 col=1.-exp(-col*1.35);float al=clamp(max(col.r,max(col.g,col.b))*1.4,0.,1.);if(d<rs)al=1.;
 gl_FragColor=vec4(col,al);}`;

/** "Buraco negro": disco de acreção em shader. Falando o disco explode em brilho, solta jatos e ondas de choque. */
export function createBlackHoleRenderer() {
  const mood = createMood();
  let gl = null;
  let surface = null;
  let uniforms = null;
  let spinPhase = 0;
  let failed = false;

  function setup() {
    if (gl || failed) return gl;
    const doc = globalThis.document;
    surface = doc?.createElement?.('canvas') || null;
    gl = surface?.getContext?.('webgl', { premultipliedAlpha: false, antialias: false }) || null;
    if (!gl) { failed = true; return null; }
    const compile = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
    const program = gl.createProgram();
    gl.attachShader(program, compile(gl.VERTEX_SHADER, 'attribute vec2 p;void main(){gl_Position=vec4(p,0,1);}'));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, BLACK_HOLE_FS));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) { failed = true; gl = null; return null; }
    gl.useProgram(program);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    uniforms = {};
    for (const key of ['res', 't', 'ouv', 'fal', 'trab', 'lv', 'sp', 'ca', 'cb']) uniforms[key] = gl.getUniformLocation(program, key);
    return gl;
  }

  return {
    draw(ctx, { t, w, h, state, level, accent }) {
      const { dt } = mood.update(t, state, level);
      const { S } = mood;
      spinPhase += dt * (1 + S.trab * 4.5 - S.ouv * 0.4 + S.fal * level * 1.8);
      ctx.clearRect(0, 0, w, h);
      if (!setup()) return { core: 0 };
      // Resolução reduzida: o shader é pesado e o canvas é esticado sem perder muito.
      const side = Math.min(280, Math.round(Math.min(w, h))); // esticado depois: 280 px bastam e custam metade de 420
      if (surface.width !== side) { surface.width = side; surface.height = side; }
      gl.viewport(0, 0, side, side);
      gl.uniform2f(uniforms.res, side, side);
      gl.uniform1f(uniforms.t, t);
      gl.uniform1f(uniforms.sp, spinPhase);
      gl.uniform1f(uniforms.ouv, S.ouv);
      gl.uniform1f(uniforms.fal, S.fal);
      gl.uniform1f(uniforms.trab, S.trab);
      gl.uniform1f(uniforms.lv, level);
      gl.uniform3f(uniforms.ca, ...accent.main.map((c) => c / 255));
      gl.uniform3f(uniforms.cb, ...accent.soft.map((c) => c / 255));
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      const size = Math.min(w, h);
      ctx.drawImage(surface, (w - size) / 2, (h - size) / 2, size, size);
      return { core: size * 0.15 };
    },
  };
}

// ---------------------------------------------------------------------------------------------------------------
// Anéis de dados, Núcleo de plasma, Toro de luz e Fitas quânticas. Cada um aceita { layer: true } para ser desenhado
// por cima de outro (não limpa a tela e não desenha o próprio núcleo): é assim que createOrbStack junta orbes.
// ---------------------------------------------------------------------------------------------------------------
const LILAC = [216, 180, 254];

/** "Anéis de dados": três discos de blocos de informação em volta do núcleo. */
export function createDataRingsRenderer({ layer = false } = {}) {
  const mood = createMood();
  const segs = [0, 1, 2].map(() => Array.from({ length: 64 }, () => ({ w: 0.3 + Math.random() * 0.7, h: Math.random() })));
  const tilts = [[1.15, 0.3], [1.25, -0.5], [1.05, 1.2]];
  let spin = 0;
  return {
    draw(ctx, { t, w, h, state, level, accent }) {
      const { dt } = mood.update(t, state, level);
      const { S } = mood;
      const hot = S.fal * level;
      const R = Math.min(w, h) * 0.44;
      spin += dt * (0.36 + S.trab * 3 - S.ouv * 0.18 + hot * 1.2);
      if (!layer) ctx.clearRect(0, 0, w, h);
      ctx.save();
      ctx.translate(w / 2, h / 2);
      ctx.globalCompositeOperation = 'lighter';
      if (!layer) glow(ctx, 0, 0, R * (0.55 + hot * 0.3), mix(accent.main, PINK, hot), 0.45 + hot * 0.3);
      for (let k = 0; k < 3; k += 1) {
        const tl = lerp(tilts[k][0], 1.38, S.ouv);
        const rz = lerp(tilts[k][1], 0, S.ouv);
        const rr = R * (0.5 + k * 0.13) * (1 + hot * 0.12);
        const sp = spin * (k % 2 && S.trab > 0.3 ? -1.6 : 1) * (1 + k * 0.3);
        const sy = Math.cos(tl);
        ctx.save();
        ctx.rotate(rz);
        ctx.strokeStyle = rgba(LILAC, 0.18);
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.ellipse(0, 0, rr, rr * sy, 0, 0, TAU); ctx.stroke();
        for (let i = 0; i < 64; i += 1) {
          const a0 = (i / 64) * TAU + sp;
          const s = segs[k][i];
          const front = Math.sin(a0) > 0;
          const lit = hot * Math.max(0, Math.sin(i * 0.4 - t * 8 + k)) + S.trab * (Math.sin(i * 0.9 + t * 14) > 0.7 ? 1 : 0)
            + S.ouv * Math.pow(Math.max(0, Math.cos(a0 - t * 2)), 20);
          ctx.strokeStyle = lit > 0.3
            ? rgba(S.trab > S.fal ? CYAN : [255, 220, 250], 0.5 + lit * 0.5)
            : rgba(k === 1 ? CYAN : accent.soft, (front ? 0.55 : 0.18) * (0.5 + s.h * 0.5));
          ctx.lineWidth = R * 0.035 * (0.6 + s.h) * (1 + hot * 0.8) * (front ? 1 : 0.6);
          ctx.beginPath(); ctx.ellipse(0, 0, rr, rr * sy, 0, a0, a0 + (TAU / 64) * s.w); ctx.stroke();
        }
        ctx.restore();
      }
      if (!layer) {
        const core = R * 0.26 * (1 + hot * 0.4);
        const g = ctx.createRadialGradient(-R * 0.05, -R * 0.06, 0, 0, 0, core);
        g.addColorStop(0, '#fff');
        g.addColorStop(0.35, rgba(mix(accent.soft, PINK, hot), 0.95));
        g.addColorStop(1, rgba(accent.main, 0));
        ctx.fillStyle = g;
        dot(ctx, 0, 0, core);
      }
      ctx.restore();
      return { core: R * 0.26 };
    },
  };
}

/** "Toro de luz": rosquinha de partículas correndo em espiral. */
export function createTorusRenderer({ layer = false } = {}) {
  const mood = createMood();
  // 700 (350 na junção): com 1400 o toro ficava em ~44 quadros por segundo
  const parts = Array.from({ length: layer ? 350 : 700 }, () => ({ u: Math.random() * TAU, v: Math.random() * TAU, s: 0.3 + Math.random(), c: Math.random() }));
  let yaw = 0;
  let roll = 0;
  return {
    draw(ctx, { t, w, h, state, level, accent }) {
      const { dt } = mood.update(t, state, level);
      const { S } = mood;
      const hot = S.fal * level;
      const R = Math.min(w, h) * 0.44;
      const k60 = dt * 60;
      yaw += (0.004 + S.trab * 0.02) * k60;
      roll += S.trab * 0.015 * k60;
      if (!layer) ctx.clearRect(0, 0, w, h);
      ctx.save();
      ctx.translate(w / 2, h / 2);
      ctx.globalCompositeOperation = 'lighter';
      if (!layer) glow(ctx, 0, 0, R * 0.7, accent.main, 0.3 + hot * 0.3);
      const Rm = R * 0.48;
      const rm = R * (0.17 + hot * 0.12 - S.ouv * 0.04);
      const flow = (0.01 + S.trab * 0.05 + hot * 0.02 - S.ouv * 0.006) * k60;
      for (const p of parts) {
        p.u += flow * p.s;
        p.v += flow * 2.2 * p.s;
        const rr = rm * (1 + hot * 0.5 * Math.max(0, Math.sin(p.u * 3 - t * 8)));
        const ring = Rm + rr * Math.cos(p.v);
        const v = rotX(rotY([ring * Math.cos(p.u), rr * Math.sin(p.v), ring * Math.sin(p.u)], yaw + VY()), 1.05 + roll + VX());
        const z = v[2] / R + 0.7;
        const lit = hot * Math.max(0, Math.sin(p.u * 4 - t * 10));
        const base = S.trab > 0.5 ? mix(CYAN, LILAC, p.c) : mix(accent.main, PINK, p.c * 0.6 + lit);
        ctx.fillStyle = rgba(mix(base, WHITE, lit * 0.6), (0.2 + z * 0.45) * (0.6 + p.c * 0.4) + lit * 0.4);
        const size = 0.8 + z * 1.3 + lit * 1.5;
        ctx.fillRect(v[0], v[1], size, size);
      }
      if (!layer) glow(ctx, 0, 0, R * (0.12 + hot * 0.15), [255, 240, 255], 0.9);
      ctx.restore();
      return { core: R * 0.12 };
    },
  };
}

/** "Fitas quânticas": três fitas de luz desenhando órbitas em 3D, com rastro. */
export function createRibbonsRenderer({ layer = false } = {}) {
  const mood = createMood();
  let yaw = 0;
  let clock = 0;
  return {
    draw(ctx, { t, w, h, state, level, accent }) {
      const { dt } = mood.update(t, state, level);
      const { S } = mood;
      const hot = S.fal * level;
      const R = Math.min(w, h) * 0.44;
      yaw += dt * (0.36 + S.trab * 1.2);
      clock += dt * (1 + S.trab * 2.5);
      if (!layer) ctx.clearRect(0, 0, w, h);
      ctx.save();
      ctx.translate(w / 2, h / 2);
      ctx.globalCompositeOperation = 'lighter';
      if (!layer) glow(ctx, 0, 0, R * 0.6, accent.main, 0.25 + hot * 0.3);
      ctx.lineCap = 'round';
      const colors = [accent.soft, PINK, [125, 211, 252]];
      for (let k = 0; k < 3; k += 1) {
        const A = R * (0.62 - S.ouv * 0.25) * (1 + hot * 0.25);
        const col = S.trab > 0.5 ? CYAN : colors[k];
        let prev = null;
        for (let i = 0; i < 70; i += 1) {
          const u = clock * 1.2 - i * 0.035 + k * 2.1;
          const p = rotX(rotY([Math.sin(u * 2 + k) * A, Math.sin(u * 3 + k * 0.7) * A * 0.7, Math.cos(u * 2 + k) * A], yaw + k + VY()), 0.5 + VX());
          if (prev) {
            const fade = 1 - i / 70;
            ctx.strokeStyle = rgba(mix(col, WHITE, hot * fade * 0.5), fade * (0.5 + hot * 0.4));
            ctx.lineWidth = fade * (3 + hot * 7) * (0.6 + (p[2] / R + 1) * 0.4);
            ctx.beginPath(); ctx.moveTo(prev[0], prev[1]); ctx.lineTo(p[0], p[1]); ctx.stroke();
          }
          prev = p;
        }
      }
      if (!layer) glow(ctx, 0, 0, R * (0.14 + hot * 0.16), [255, 240, 255], 0.95);
      ctx.restore();
      return { core: R * 0.14 };
    },
  };
}

const PLASMA_FS = `precision mediump float;uniform vec2 res;uniform float t,ouv,fal,trab,lv,sp,lay;uniform vec3 ca,cb;
float h(vec3 p){return fract(sin(dot(p,vec3(127.1,311.7,74.7)))*43758.5453);}
float n(vec3 p){vec3 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);return mix(mix(mix(h(i),h(i+vec3(1,0,0)),f.x),mix(h(i+vec3(0,1,0)),h(i+vec3(1,1,0)),f.x),f.y),mix(mix(h(i+vec3(0,0,1)),h(i+vec3(1,0,1)),f.x),mix(h(i+vec3(0,1,1)),h(i+vec3(1,1,1)),f.x),f.y),f.z);}
float fbm(vec3 p){float v=0.,a=.5;for(int i=0;i<5;i++){v+=a*n(p);p*=2.02;a*=.5;}return v;}
void main(){vec2 uv=(gl_FragCoord.xy-.5*res)/res.y*2.2;float hot=fal*lv;float R=(.55-lay*.22)*(1.+hot*.18);float d=length(uv);vec3 col=vec3(0.);float ang=atan(uv.y,uv.x);
 if(d<R){float z=sqrt(R*R-d*d)/R;vec3 p=vec3(uv/R,z);float c=cos(sp*.3),s=sin(sp*.3);p.xz=mat2(c,-s,s,c)*p.xz;
  float f=fbm(p*2.5+vec3(0.,0.,t*(.2+hot*1.5)));float f2=fbm(p*6.-vec3(t*.4));
  float ripple=ouv*pow(.5+.5*sin(d/R*18.+t*7.),6.);float band=trab*pow(.5+.5*sin(p.y*10.+sp*2.),8.);
  vec3 base=mix(ca*.45,mix(cb,vec3(.95,.35,.9),.4),f)+vec3(1.,.9,1.)*pow(f2,3.)*(1.+hot*3.);
  col=base*(.35+z*.8)+vec3(.7,.6,1.)*ripple*.8+vec3(.3,.95,1.)*band;col*=1.+hot*1.2;col+=vec3(1.,.6,1.)*pow(1.-z,2.5)*(1.2+hot*2.);}
 col+=vec3(.95,.5,1.)*exp(-abs(d-R)*40.)*(.8+hot*2.);
 float fl=fbm(vec3(ang*3.,d*4.-t*(1.+hot*4.),t*.3));col+=vec3(1.,.45,.9)*smoothstep(R,R*1.02,d)*exp(-(d-R)*(9.-hot*6.))*pow(fl,2.)*(.6+hot*4.);
 col+=ca*exp(-d*2.2)*.25*(1.-lay);
 if(trab>.01){float k=exp(-abs(d-R*1.35)*80.)*step(.6,fract(ang/6.2832*16.+sp*.3));col+=vec3(.3,.95,1.)*k*trab;}
 col=1.-exp(-col*1.3);float al=d<R?1.:clamp(max(col.r,max(col.g,col.b))*1.3,0.,1.);gl_FragColor=vec4(col*al,al);}`;

/** "Núcleo de plasma": esfera de energia em shader. Em camada (junções) fica menor e serve de núcleo. */
export function createPlasmaRenderer({ layer = false } = {}) {
  const mood = createMood();
  let gl = null;
  let surface = null;
  let uniforms = null;
  let spinPhase = 0;
  let failed = false;
  function setup() {
    if (gl || failed) return gl;
    surface = globalThis.document?.createElement?.('canvas') || null;
    gl = surface?.getContext?.('webgl', { premultipliedAlpha: true, antialias: false }) || null;
    if (!gl) { failed = true; return null; }
    const compile = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
    const program = gl.createProgram();
    gl.attachShader(program, compile(gl.VERTEX_SHADER, 'attribute vec2 p;void main(){gl_Position=vec4(p,0,1);}'));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, PLASMA_FS));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) { failed = true; gl = null; return null; }
    gl.useProgram(program);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    uniforms = {};
    for (const key of ['res', 't', 'ouv', 'fal', 'trab', 'lv', 'sp', 'lay', 'ca', 'cb']) uniforms[key] = gl.getUniformLocation(program, key);
    return gl;
  }
  return {
    draw(ctx, { t, w, h, state, level, accent }) {
      const { dt } = mood.update(t, state, level);
      const { S } = mood;
      spinPhase += dt * (1 + S.trab * 4 + S.fal * level * 2);
      if (!layer) ctx.clearRect(0, 0, w, h);
      if (!setup()) return { core: 0 };
      const side = Math.min(260, Math.round(Math.min(w, h)));
      if (surface.width !== side) { surface.width = side; surface.height = side; }
      gl.viewport(0, 0, side, side);
      gl.uniform2f(uniforms.res, side, side);
      gl.uniform1f(uniforms.t, t);
      gl.uniform1f(uniforms.sp, spinPhase);
      gl.uniform1f(uniforms.ouv, S.ouv);
      gl.uniform1f(uniforms.fal, S.fal);
      gl.uniform1f(uniforms.trab, S.trab);
      gl.uniform1f(uniforms.lv, level);
      gl.uniform1f(uniforms.lay, layer ? 1 : 0);
      gl.uniform3f(uniforms.ca, ...accent.main.map((c) => c / 255));
      gl.uniform3f(uniforms.cb, ...accent.soft.map((c) => c / 255));
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      const size = Math.min(w, h);
      ctx.drawImage(surface, (w - size) / 2, (h - size) / 2, size, size);
      return { core: size * 0.25 };
    },
  };
}

const STACK_PARTS = { plasma: createPlasmaRenderer, torus: createTorusRenderer, ribbons: createRibbonsRenderer, rings: createDataRingsRenderer };
// Ordem de pintura de uma junção: o plasma é o núcleo (fundo), depois toro, fitas e por último os anéis na frente.
const STACK_ORDER = ['plasma', 'torus', 'ribbons', 'rings'];

/** Junta orbes num só. Com plasma, ele vira o núcleo pequeno e tudo é camada; sem plasma, o primeiro é desenhado inteiro. */
export function createOrbStack(names) {
  const picked = STACK_ORDER.filter((name) => names.includes(name));
  const withCore = picked.includes('plasma');
  const layers = picked.map((name, i) => STACK_PARTS[name]({ layer: withCore || i > 0 }));
  return {
    draw(ctx, frame) {
      if (withCore) ctx.clearRect(0, 0, frame.w, frame.h);
      let out = { core: 0 };
      layers.forEach((renderer) => { out = renderer.draw(ctx, frame) || out; });
      return out;
    },
  };
}
