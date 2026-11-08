/* Tela de início "Portal": monta anel da raposa, relógio, canto, faixa, fundo com profundidade e a engrenagem com as
   opções (salvas em localStorage KIRA_PORTAL). O botão de iniciar só entra, com animação e som, quando o botão já
   foi liberado pelo index.html e Voz e Cérebro estão ok (ou 8 s depois de liberado, para nunca prender a pessoa).
   Tudo para quando a tela de início some, para não gastar processador. */
(function () {
  const screen = document.getElementById('kira-start-screen');
  const welcome = document.getElementById('welcome-screen');
  if (!screen) return;
  const root = document.documentElement;
  const STORE = 'KIRA_PORTAL';
  const DEFAULTS = { bg: '0', ring: '1', clock: '1', info: '0', btn: '1', sound: '1', foot: '0' };
  const OPTIONS = [
    ['bg', 'FUNDO', [['0', 'Simples (leve, parado)'], ['1', 'Rede neural 3D'], ['2', 'Estrelas em profundidade'], ['3', 'Nebulosa com camadas'], ['4', 'Túnel de anéis'], ['5', 'Galáxia inclinada']]],
    ['ring', 'ANEL DA RAPOSA', [['1', 'Anéis girando'], ['2', 'Giroscópio 3D'], ['3', 'Radar']]],
    ['btn', 'APARECIMENTO DO BOTÃO', [['1', 'Linha de energia'], ['2', 'Materialização'], ['3', 'Escaneamento'], ['4', 'Portal'], ['5', 'Glitch']]],
    ['sound', 'SOM DO BOTÃO', [['1', 'Energia'], ['2', 'Cristal'], ['3', 'Whoosh'], ['4', 'Pulso grave'], ['5', 'Arpejo digital'], ['6', 'Coral'], ['0', 'Sem som']]],
    ['clock', 'RELÓGIO', [['1', 'Digital'], ['2', 'Analógico neon'], ['3', 'Anel dos segundos'], ['4', 'Fino'], ['5', 'Placas'], ['0', 'Nenhum']]],
    ['info', 'NÚCLEO, CHAVE E VOZ', [['0', 'Esconder'], ['1', 'Linha lateral'], ['2', 'Etiquetas de vidro'], ['3', 'Barras']]],
    ['foot', 'FAIXA DE BAIXO', [['1', 'Onda de voz'], ['2', 'Status'], ['0', 'Nada']]],
  ];
  let prefs = { ...DEFAULTS };
  try { Object.assign(prefs, JSON.parse(localStorage.getItem(STORE) || '{}')); } catch (_) { /* fica o padrão */ }
  // 2026-11-07: tela inicial mais leve por padrão (fundo parado, sem faixa de baixo); dá para voltar na engrenagem
  if (!prefs.lite) { prefs.bg = '0'; prefs.foot = '0'; prefs.lite = 1; try { localStorage.setItem(STORE, JSON.stringify(prefs)); } catch (_) {} }
  const applyPrefs = () => { for (const [k] of OPTIONS) root.dataset[`kp${k[0].toUpperCase()}${k.slice(1)}`] = prefs[k]; };
  root.dataset.kpOn = '1';
  applyPrefs();

  const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
  const ticks = (r1, r2, n, rb) => Array.from({ length: n }, (_, i) => {
    const a = (i / n) * Math.PI * 2; const big = i % 5 === 0; const r = big ? rb : r2;
    return `<line x1="${(100 + Math.cos(a) * r1).toFixed(1)}" y1="${(100 + Math.sin(a) * r1).toFixed(1)}" x2="${(100 + Math.cos(a) * r).toFixed(1)}" y2="${(100 + Math.sin(a) * r).toFixed(1)}" stroke="rgba(216,180,254,${big ? 0.7 : 0.3})" stroke-width=".6"/>`;
  }).join('');

  // ---- anel em volta da raposa ----
  const avatar = screen.querySelector('.kira-avatar-container');
  if (avatar) {
    avatar.prepend(el('div', 'kp-disc'));
    avatar.prepend(el('div', 'kp-halo kp-h3', `<svg viewBox="0 0 200 200"><defs><linearGradient id="kp-sw" x1="0" x2="1"><stop offset="0" stop-color="rgba(217,70,239,0)"/><stop offset="1" stop-color="rgba(217,70,239,.55)"/></linearGradient></defs>
      <circle cx="100" cy="100" r="96" fill="none" stroke="rgba(168,85,247,.35)" stroke-width=".6"/><circle cx="100" cy="100" r="80" fill="none" stroke="rgba(168,85,247,.25)" stroke-width=".5"/><circle cx="100" cy="100" r="64" fill="none" stroke="rgba(168,85,247,.2)" stroke-width=".5"/>
      <g class="kp-sp" style="--d:4s"><path d="M100 100 L196 100 A96 96 0 0 0 183 52 Z" fill="url(#kp-sw)"/><line x1="100" y1="100" x2="196" y2="100" stroke="#f0abfc" stroke-width="1.2"/></g>
      <g class="kp-sp kp-rv" style="--d:60s">${ticks(86, 89, 60, 91)}</g></svg>`));
    avatar.prepend(el('div', 'kp-halo kp-h2', '<div class="kp-gy a"></div><div class="kp-gy b"></div><div class="kp-gy c"></div>'));
    avatar.prepend(el('div', 'kp-halo kp-h1', `<svg viewBox="0 0 200 200">
      <g class="kp-sp" style="--d:46s"><circle cx="100" cy="100" r="99" fill="none" stroke="rgba(168,85,247,.25)" stroke-width=".4"/><g fill="rgba(216,180,254,.7)"><circle cx="100" cy="1" r="1.3"/><circle cx="199" cy="100" r="1.3"/><circle cx="100" cy="199" r="1.3"/><circle cx="1" cy="100" r="1.3"/></g></g>
      <g class="kp-sp" style="--d:22s"><circle cx="100" cy="100" r="93" fill="none" stroke="rgba(168,85,247,.4)" stroke-width=".6" stroke-dasharray="1 3"/><path d="M100 7 A93 93 0 0 1 193 100" fill="none" stroke="#d946ef" stroke-width="1.6"/><circle cx="193" cy="100" r="2.4" fill="#fff"/></g>
      <g class="kp-sp kp-rv" style="--d:14s"><circle cx="100" cy="100" r="82" fill="none" stroke="rgba(94,234,212,.6)" stroke-width="1" stroke-dasharray="30 12"/></g>
      <g class="kp-sp" style="--d:7s"><path d="M100 24 A76 76 0 0 1 166 62" fill="none" stroke="rgba(240,171,252,.95)" stroke-width="1.2"/><path d="M100 176 A76 76 0 0 1 34 138" fill="none" stroke="rgba(94,234,212,.95)" stroke-width="1.2"/><circle cx="166" cy="62" r="1.8" fill="#fff"/><circle cx="34" cy="138" r="1.8" fill="#fff"/></g>
      <g class="kp-sp kp-rv" style="--d:30s">${ticks(86, 89, 60, 91)}</g></svg>`));
  }
  // Cada anel vira um <svg> próprio girando em camada (o grupo dentro de um SVG só é redesenhado inteiro a cada quadro)
  screen.querySelectorAll('.kp-halo svg').forEach((svg) => {
    svg.querySelectorAll(':scope > g.kp-sp').forEach((group) => {
      const own = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      own.setAttribute('viewBox', '0 0 200 200');
      own.setAttribute('class', group.getAttribute('class'));
      own.setAttribute('style', group.getAttribute('style') || '');
      group.removeAttribute('class');
      group.removeAttribute('style');
      own.appendChild(group);
      svg.parentElement.appendChild(own);
    });
  });
  const label = screen.querySelector('.kira-start-label');
  if (label) label.textContent = 'ASSISTENTE INTELIGENTE';

  // ---- relógio, canto, faixa, engrenagem ----
  const dial = Array.from({ length: 12 }, (_, i) => { const a = (i / 12) * Math.PI * 2; const big = i % 3 === 0;
    return `<line x1="${(50 + Math.cos(a) * 40).toFixed(1)}" y1="${(50 + Math.sin(a) * 40).toFixed(1)}" x2="${(50 + Math.cos(a) * 44).toFixed(1)}" y2="${(50 + Math.sin(a) * 44).toFixed(1)}" stroke="rgba(240,171,252,${big ? 0.9 : 0.4})" stroke-width="${big ? 1.6 : 0.8}"/>`; }).join('');
  const clock = el('div', 'kp-clock', `<div class="kp-c1"><div class="t" data-kp-hm></div><div class="d" data-kp-date></div></div>
    <div class="kp-c2"><svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="46" fill="rgba(20,5,40,.5)" stroke="rgba(217,70,239,.7)" stroke-width="1.5"/>${dial}<line data-kp-h x1="50" y1="50" x2="50" y2="28" stroke="#fff" stroke-width="3" stroke-linecap="round"/><line data-kp-m x1="50" y1="50" x2="50" y2="16" stroke="#e9d5ff" stroke-width="2" stroke-linecap="round"/><line data-kp-s x1="50" y1="56" x2="50" y2="10" stroke="#e879f9" stroke-width="1"/><circle cx="50" cy="50" r="2.5" fill="#e879f9"/></svg></div>
    <div class="kp-c3"><svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="44" fill="none" stroke="rgba(255,255,255,.1)" stroke-width="4"/><circle data-kp-sec cx="50" cy="50" r="44" fill="none" stroke="#d946ef" stroke-width="4" stroke-linecap="round" pathLength="60" stroke-dasharray="0 60" transform="rotate(-90 50 50)"/></svg><div class="t"><span data-kp-hm></span><small data-kp-short></small></div></div>
    <div class="kp-c4"><div class="t" data-kp-hm></div><div class="d" data-kp-date></div></div>
    <div class="kp-c5" data-kp-flip></div>`);
  const info = el('div', 'kp-info', `<div class="kp-i1">KIRA // NÚCLEO <b>V8</b><span>CHAVE <b data-kp-key>—</b></span><span>VOZ <b data-kp-link>—</b></span></div>
    <div class="kp-i2"><span>NÚCLEO <b>V8</b></span><span>CHAVE <b data-kp-key>—</b></span><span>VOZ <b data-kp-link>—</b></span></div>
    <div class="kp-i3"><span>NÚCLEO <em style="--v:100%"></em></span><span>CHAVE <em data-kp-keybar style="--v:25%"></em></span><span>VOZ <em data-kp-voicebar style="--v:30%"></em></span></div>`);
  const foot1 = el('div', 'kp-foot kp-f1', Array.from({ length: 44 }, (_, i) => `<b style="animation-delay:${(-i * 0.09).toFixed(2)}s;animation-duration:${(0.9 + (i % 5) * 0.15).toFixed(2)}s"></b>`).join(''));
  const foot2 = el('div', 'kp-foot kp-f2', '<span data-kp-st="livekit">VOZ</span><span data-kp-st="brain">CÉREBRO</span><span class="ok">NÚCLEO V8</span>');
  const gear = el('button', 'kp-gear', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/></svg>');
  gear.type = 'button';
  gear.setAttribute('aria-label', 'Personalizar a tela de início');
  gear.setAttribute('aria-expanded', 'false');
  const panel = el('div', 'kp-panel', '<h3>PERSONALIZAR</h3><p class="hint">Tela de iniciar sessão</p>');
  panel.hidden = true;
  for (const [key, title, opts] of OPTIONS) {
    const grp = el('div', 'kp-grp', `<span>${title}</span>`);
    const box = el('div', 'kp-opts');
    for (const [v, name] of opts) {
      const b = el('button', null, name);
      b.type = 'button';
      b.dataset.v = v;
      b.setAttribute('aria-pressed', String(prefs[key] === v));
      b.addEventListener('click', () => {
        prefs[key] = v;
        try { localStorage.setItem(STORE, JSON.stringify(prefs)); } catch (_) { /* vale até fechar */ }
        applyPrefs();
        box.querySelectorAll('button').forEach((o) => o.setAttribute('aria-pressed', String(o === b)));
        if (key === 'sound') play(v, true);
        if (key === 'btn' && shown) { shown = false; idle?.classList.remove('kp-in'); setTimeout(check, 60); }
        if (key === 'bg') build();
      });
      box.appendChild(b);
    }
    grp.appendChild(box);
    panel.appendChild(grp);
  }
  gear.addEventListener('click', () => { panel.hidden = !panel.hidden; gear.setAttribute('aria-expanded', String(!panel.hidden)); });
  const bg = el('canvas');
  bg.id = 'kp-bg';
  bg.setAttribute('aria-hidden', 'true');
  screen.prepend(bg);
  screen.append(clock, info, foot1, foot2, gear, panel);

  // ---- área do botão: efeitos de entrada ----
  const idle = document.getElementById('kiraStartIdleArea');
  if (idle) idle.append(el('div', 'kp-fx', '<div class="kp-halo2"></div><div class="kp-line"></div><i class="kp-ring"></i><div class="kp-laser"></div>'));

  // ---- sons de entrada (sintetizados, sem arquivo) ----
  let ac = null;
  function audio() {
    if (!ac) { try { ac = new (window.AudioContext || window.webkitAudioContext)(); } catch (_) { return null; } }
    if (ac.state === 'suspended') ac.resume().catch(() => {});
    return ac;
  }
  function bus(vol = 0.22) {
    const o = ac.createGain(); o.gain.value = vol; o.connect(ac.destination);
    const dl = ac.createDelay(); dl.delayTime.value = 0.18; const fb = ac.createGain(); fb.gain.value = 0.3; dl.connect(fb); fb.connect(dl); dl.connect(o);
    return { o, dl };
  }
  function tone(dst, f, type, st, dur, vol, dl) {
    const o = ac.createOscillator(); const g = ac.createGain(); o.type = type; o.frequency.value = f;
    g.gain.setValueAtTime(0, st); g.gain.linearRampToValueAtTime(vol, st + 0.015); g.gain.exponentialRampToValueAtTime(0.001, st + dur);
    o.connect(g); g.connect(dst); if (dl) g.connect(dl); o.start(st); o.stop(st + dur + 0.05);
  }
  const SOUNDS = {
    1(t0) { const { o, dl } = bus(); const sw = ac.createOscillator(); const sg = ac.createGain(); const bp = ac.createBiquadFilter(); sw.type = 'sawtooth'; bp.type = 'bandpass'; bp.Q.value = 6;
      sw.frequency.setValueAtTime(90, t0); sw.frequency.exponentialRampToValueAtTime(900, t0 + 0.9); bp.frequency.setValueAtTime(300, t0); bp.frequency.exponentialRampToValueAtTime(3000, t0 + 0.9);
      sg.gain.setValueAtTime(0, t0); sg.gain.linearRampToValueAtTime(0.12, t0 + 0.5); sg.gain.exponentialRampToValueAtTime(0.001, t0 + 1.1); sw.connect(bp); bp.connect(sg); sg.connect(o); sw.start(t0); sw.stop(t0 + 1.2);
      [523.25, 659.25, 783.99, 1046.5, 1318.5].forEach((f, i) => tone(o, f, i % 2 ? 'triangle' : 'sine', t0 + 0.8 + i * 0.07, 1.8, 0.14, dl)); },
    2(t0) { const { o, dl } = bus(0.25); [1567.98, 2093, 2637, 3135.96].forEach((f, i) => { tone(o, f, 'sine', t0 + i * 0.09, 2.2, 0.12, dl); tone(o, f * 2.76, 'sine', t0 + i * 0.09, 0.6, 0.03, dl); }); },
    3(t0) { const { o } = bus(0.3); const buf = ac.createBuffer(1, ac.sampleRate * 1.5, ac.sampleRate); const d = buf.getChannelData(0); for (let i = 0; i < d.length; i += 1) d[i] = Math.random() * 2 - 1;
      const n = ac.createBufferSource(); n.buffer = buf; const bp = ac.createBiquadFilter(); bp.type = 'bandpass'; bp.Q.value = 2; bp.frequency.setValueAtTime(200, t0); bp.frequency.exponentialRampToValueAtTime(5000, t0 + 0.7); bp.frequency.exponentialRampToValueAtTime(800, t0 + 1.3);
      const g = ac.createGain(); g.gain.setValueAtTime(0, t0); g.gain.linearRampToValueAtTime(0.5, t0 + 0.6); g.gain.exponentialRampToValueAtTime(0.001, t0 + 1.4); n.connect(bp); bp.connect(g); g.connect(o); n.start(t0); n.stop(t0 + 1.5); tone(o, 880, 'sine', t0 + 0.65, 0.9, 0.08); },
    4(t0) { const { o, dl } = bus(0.35); const b = ac.createOscillator(); const g = ac.createGain(); b.type = 'sine'; b.frequency.setValueAtTime(110, t0); b.frequency.exponentialRampToValueAtTime(45, t0 + 0.8);
      g.gain.setValueAtTime(0.6, t0); g.gain.exponentialRampToValueAtTime(0.001, t0 + 0.9); b.connect(g); g.connect(o); b.start(t0); b.stop(t0 + 1); tone(o, 1760, 'triangle', t0 + 0.15, 1.2, 0.06, dl); tone(o, 2637, 'sine', t0 + 0.25, 1.2, 0.04, dl); },
    5(t0) { const { o, dl } = bus(0.2); [523.25, 659.25, 783.99, 1046.5, 783.99, 1046.5, 1318.5, 1567.98].forEach((f, i) => tone(o, f, 'square', t0 + i * 0.06, 0.18, 0.05, dl)); tone(o, 2093, 'sine', t0 + 0.5, 1.2, 0.08, dl); },
    6(t0) { const { o, dl } = bus(0.25); [261.63, 329.63, 392, 523.25].forEach((f) => [-4, 0, 5].forEach((dt) => { const os = ac.createOscillator(); const g = ac.createGain(); const lp = ac.createBiquadFilter();
      os.type = 'sawtooth'; os.frequency.value = f; os.detune.value = dt * 3; lp.type = 'lowpass'; lp.frequency.setValueAtTime(400, t0); lp.frequency.linearRampToValueAtTime(2200, t0 + 1);
      g.gain.setValueAtTime(0, t0); g.gain.linearRampToValueAtTime(0.025, t0 + 0.6); g.gain.exponentialRampToValueAtTime(0.001, t0 + 2.4); os.connect(lp); lp.connect(g); g.connect(o); g.connect(dl); os.start(t0); os.stop(t0 + 2.5); })); },
  };
  function play(key) {
    if (!SOUNDS[key] || !audio()) return;
    try { SOUNDS[key](ac.currentTime + 0.04); } catch (_) { /* sem áudio: segue mudo */ }
  }

  // ---- entra o botão: liberado pelo index.html + Voz e Cérebro ok ----
  const startBtn = document.getElementById('start-btn');
  let shown = false;
  let unlockedAt = 0;
  const sysOk = (k) => Boolean(screen.querySelector(`.ks2-sys[data-k="${k}"].ok`));
  function check() {
    if (!idle || !startBtn) return;
    const unlocked = !startBtn.disabled && !startBtn.hidden && startBtn.style.display !== 'none';
    if (!unlocked) { unlockedAt = 0; if (shown) { shown = false; idle.classList.remove('kp-in'); } return; }
    if (!unlockedAt) unlockedAt = performance.now();
    const ready = (sysOk('livekit') && sysOk('brain')) || performance.now() - unlockedAt > 8000;
    if (ready && !shown) {
      shown = true;
      idle.classList.add('kp-in');
      boost = 1;
      if (prefs.btn === '2') sparks();
      play(prefs.sound);
    }
  }
  if (startBtn) new MutationObserver(check).observe(startBtn, { attributes: true, attributeFilter: ['disabled', 'hidden', 'style', 'data-start-unlocked'] });

  function sparks() {
    const r = idle.getBoundingClientRect(); const cx = r.left + r.width / 2; const cy = r.top + r.height / 2;
    const ps = Array.from({ length: 70 }, () => { const a = Math.random() * 6.28; const d = 60 + Math.random() * 180; return { x: cx + Math.cos(a) * d, y: cy + Math.sin(a) * d, tx: cx + (Math.random() - 0.5) * r.width, ty: cy + (Math.random() - 0.5) * r.height }; });
    const ov = el('canvas'); ov.style.cssText = 'position:fixed;inset:0;width:100vw;height:100vh;pointer-events:none;z-index:9999';
    document.body.appendChild(ov); ov.width = innerWidth; ov.height = innerHeight; const o = ov.getContext('2d'); const t0 = performance.now() + 900;
    (function f(now) {
      const k = Math.max(0, Math.min(1, (now - t0) / 1100)); const e = 1 - Math.pow(1 - k, 3); o.clearRect(0, 0, ov.width, ov.height); o.globalCompositeOperation = 'lighter';
      for (const p of ps) { const X = p.x + (p.tx - p.x) * e; const Y = p.y + (p.ty - p.y) * e; const g = o.createRadialGradient(X, Y, 0, X, Y, 6); g.addColorStop(0, `rgba(255,240,255,${1 - k * 0.6})`); g.addColorStop(1, 'rgba(217,70,239,0)'); o.fillStyle = g; o.beginPath(); o.arc(X, Y, 6, 0, 6.28); o.fill(); }
      if (k < 1) requestAnimationFrame(f); else ov.remove();
    })(performance.now());
  }

  // ---- fundos com profundidade ----
  const x = bg.getContext('2d');
  let W = 0; let H = 0; let nodes = []; let stars = []; let rings = []; let gal = []; let blobs = []; let boost = 0; let cam = 0;
  function build() {
    const dpr = 0.6; // fundo é névoa: desenhado em 60% e esticado pelo compositor (antes até 1,5x: 4x mais pixels por quadro)
    W = bg.clientWidth; H = bg.clientHeight; bg.width = W * dpr; bg.height = H * dpr; x.setTransform(dpr, 0, 0, dpr, 0, 0);
    nodes = Array.from({ length: 130 }, () => ({ x: (Math.random() - 0.5) * 2.4, y: (Math.random() - 0.5) * 1.4, z: Math.random() * 2 + 0.3, vx: (Math.random() - 0.5) * 0.0006, vy: (Math.random() - 0.5) * 0.0006, p: Math.random() * 6.28 }));
    stars = Array.from({ length: 420 }, () => ({ x: (Math.random() - 0.5) * 2, y: (Math.random() - 0.5) * 2, z: Math.random() }));
    rings = Array.from({ length: 14 }, (_, i) => ({ z: i / 14 }));
    gal = Array.from({ length: 1200 }, (_, i) => { const d = Math.pow(Math.random(), 0.6); return { a: (i % 3) * 2.094 + d * 5 + (Math.random() - 0.5) * 0.5, d, c: Math.random() }; });
    if (!blobs.length) blobs = [['124,58,237', '219,39,119'], ['76,29,149', '45,212,191'], ['192,38,211', '99,102,241']].map((cols) => {
      const c = el('canvas'); c.width = c.height = 512; const g = c.getContext('2d');
      for (let i = 0; i < 26; i += 1) { const X = Math.random() * 512; const Y = Math.random() * 512; const R = 60 + Math.random() * 140; const gr = g.createRadialGradient(X, Y, 0, X, Y, R); gr.addColorStop(0, `rgba(${cols[i % 2]},${0.06 + Math.random() * 0.09})`); gr.addColorStop(1, 'rgba(0,0,0,0)'); g.fillStyle = gr; g.fillRect(0, 0, 512, 512); }
      return c;
    });
  }
  const proj = (X, Y, Z) => { const f = 1 / Z; return [W / 2 + X * f * W * 0.45, H / 2 + Y * f * W * 0.45, f]; };
  let still = ''; // fundo '0': desenhado uma vez só (repinta só se mudar o tamanho)
  const BG = {
    0() { for (let i = 0; i < stars.length; i += 3) { const st = stars[i]; x.fillStyle = `rgba(235,220,255,${0.15 + st.z * 0.5})`; x.fillRect((st.x + 1) / 2 * W, (st.y + 1) / 2 * H, st.z * 1.6 + 0.4, st.z * 1.6 + 0.4); } },
    1(t) { cam += 0.0012; const c = Math.cos(cam); const sn = Math.sin(cam);
      const P = nodes.map((n) => { n.x += n.vx; n.y += n.vy; if (Math.abs(n.x) > 1.3) n.vx *= -1; if (Math.abs(n.y) > 0.8) n.vy *= -1; const X = n.x * c - (n.z - 1.3) * sn * 0.35; const Z = (n.z - 1.3) * c + n.x * sn * 0.35 + 1.6; return [...proj(X, n.y, Z), n]; });
      x.lineWidth = 1;
      for (let i = 0; i < P.length; i += 1) for (let j = i + 1; j < P.length; j += 1) { const a = P[i]; const b = P[j]; const dx = a[3].x - b[3].x; const dy = a[3].y - b[3].y; const dz = a[3].z - b[3].z; const d = dx * dx + dy * dy + dz * dz;
        if (d < 0.11) { const al = (1 - d / 0.11) * 0.32 * Math.min(a[2], b[2]) * 0.9; x.strokeStyle = `rgba(168,120,255,${al})`; x.beginPath(); x.moveTo(a[0], a[1]); x.lineTo(b[0], b[1]); x.stroke();
          if ((i * 7 + j) % 23 === 0) { const ph = (t * 0.4 + i * 0.13) % 1; x.fillStyle = `rgba(240,171,252,${al * 2.2})`; x.beginPath(); x.arc(a[0] + (b[0] - a[0]) * ph, a[1] + (b[1] - a[1]) * ph, 1.4 * a[2], 0, 6.28); x.fill(); } } }
      for (const p of P) { const tw = 0.6 + 0.4 * Math.sin(t * 2 + p[3].p); const r = Math.max(0.4, p[2] * 1.6); x.fillStyle = `rgba(230,210,255,${Math.min(1, p[2] * 0.6) * tw})`; x.beginPath(); x.arc(p[0], p[1], r, 0, 6.28); x.fill();
        if (p[2] > 1.2) { x.fillStyle = `rgba(217,70,239,${0.12 * tw})`; x.beginPath(); x.arc(p[0], p[1], r * 5, 0, 6.28); x.fill(); } } },
    2() { const sp = 0.0016 + boost * 0.02;
      for (const st of stars) { st.z -= sp; if (st.z <= 0.02) { st.z = 1; st.x = (Math.random() - 0.5) * 2; st.y = (Math.random() - 0.5) * 2; }
        const [px, py] = proj(st.x, st.y, st.z * 2); const [qx, qy] = proj(st.x, st.y, (st.z + sp * 8) * 2); x.strokeStyle = `rgba(${200 + st.x * 50 | 0},190,255,${Math.min(1, (1 - st.z) * 1.3)})`; x.lineWidth = Math.max(0.5, (1 - st.z) * 2.2); x.beginPath(); x.moveTo(qx, qy); x.lineTo(px, py); x.stroke(); } },
    3(t) { blobs.forEach((b, k) => { const sc = W * (0.9 + k * 0.35); const ox = (t * (k + 1) * 0.006 * 60) % W; for (const dx of [-sc, 0]) x.drawImage(b, (ox * (k + 1) * 0.4) % sc + dx - W * 0.1, H / 2 - sc / 2 + Math.sin(t * 0.1 + k) * H * 0.05, sc, sc); });
      for (let i = 0; i < stars.length; i += 2) { const st = stars[i]; const px = ((st.x + 1) / 2 * W + t * (4 + st.z * 18)) % W; const py = (st.y + 1) / 2 * H; x.fillStyle = `rgba(255,240,255,${0.2 + st.z * 0.6})`; x.fillRect(px, py, st.z * 1.8 + 0.3, st.z * 1.8 + 0.3); } },
    4(t) { for (const r of rings) { r.z -= 0.0025 + boost * 0.01; if (r.z <= 0.02) r.z = 1; const f = 1 / (r.z * 3); const R = W * 0.12 * f; const a = Math.min(1, (1 - r.z) * 1.2) * 0.6;
      x.strokeStyle = `rgba(${168 + r.z * 60 | 0},${85 + (1 - r.z) * 100 | 0},247,${a})`; x.lineWidth = Math.max(0.5, f * 1.2); x.beginPath(); x.ellipse(W / 2, H * 0.42, R, R * 0.9, 0, 0, 6.28); x.stroke();
      for (let k = 0; k < 12; k += 1) { const an = (k / 12) * 6.28 + t * 0.2; x.fillStyle = `rgba(240,171,252,${a})`; x.beginPath(); x.arc(W / 2 + Math.cos(an) * R, H * 0.42 + Math.sin(an) * R * 0.9, Math.max(0.6, f * 1.4), 0, 6.28); x.fill(); } } },
    5(t) { const cx = W / 2; const cy = H * 0.42; x.save(); x.translate(cx, cy); x.rotate(-0.25);
      for (const p of gal) { const a = p.a + t * 0.05 * (1.4 - p.d); const r = p.d * W * 0.55; const z = Math.sin(a); const b = (0.25 + p.c * 0.5) * (0.7 + 0.3 * z);
        x.fillStyle = p.d < 0.2 ? `rgba(255,235,255,${b})` : `hsla(${265 + p.c * 55},85%,70%,${b * 0.7})`; const s = (1 + z * 0.4) * (p.c > 0.9 ? 1.8 : 1.1); x.fillRect(Math.cos(a) * r, Math.sin(a) * r * 0.32, s, s); }
      x.restore(); },
  };
  // getComputedStyle a cada quadro obrigava o navegador a recalcular estilos 100x/s: confere só a cada 500 ms
  let seenAt = 0; let seen = false;
  const visible = () => {
    const now = performance.now();
    if (now - seenAt > 500) { seenAt = now; seen = !document.hidden && Boolean(welcome) && welcome.getClientRects().length > 0 && getComputedStyle(welcome).visibility !== 'hidden' && getComputedStyle(welcome).opacity !== '0'; }
    return seen;
  };
  const portalFrameMs = () => { let c = 'seguro'; try { c = localStorage.getItem('kira.fps') || 'seguro'; } catch (_) {} return c === 'seguro' ? 40 : 1000 / Number(c); };
  let frame = 0; let last = 0;
  function draw(now) {
    frame = 0;
    if (!visible()) return;
    const key = prefs.bg === '0' ? `${W}x${H}` : '';
    if (key && key === still) { frame = requestAnimationFrame(draw); return; }
    if (!key) still = '';
    if (now - last >= portalFrameMs()) {
      still = key; // Aparência > Animações: 'seguro' = 25 quadros (fundo lento por natureza)
      last = now;
      const t = now / 1000;
      const g = x.createRadialGradient(W / 2, H * 0.42, 0, W / 2, H * 0.42, W * 0.75); g.addColorStop(0, '#1d0a33'); g.addColorStop(0.5, '#0a0416'); g.addColorStop(1, '#020108');
      x.globalCompositeOperation = 'source-over'; x.fillStyle = g; x.fillRect(0, 0, W, H);
      x.globalCompositeOperation = 'lighter'; (BG[prefs.bg] || BG[1])(t); x.globalCompositeOperation = 'source-over';
      boost *= 0.97;
    }
    frame = requestAnimationFrame(draw);
  }

  // ---- relógio e dados do canto ----
  const pad = (n) => String(n).padStart(2, '0');
  const DAYS = ['DOMINGO', 'SEGUNDA', 'TERÇA', 'QUARTA', 'QUINTA', 'SEXTA', 'SÁBADO'];
  const all = (sel) => screen.querySelectorAll(sel);
  function tick() {
    const d = new Date(); const h = pad(d.getHours()); const m = pad(d.getMinutes()); const s = d.getSeconds(); const day = DAYS[d.getDay()];
    all('[data-kp-hm]').forEach((n) => { n.textContent = `${h}:${m}`; });
    all('[data-kp-date]').forEach((n) => { n.textContent = `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} · ${day}`; });
    all('[data-kp-short]').forEach((n) => { n.textContent = `${day.slice(0, 3)} ${d.getDate()}`; });
    all('[data-kp-sec]').forEach((n) => n.setAttribute('stroke-dasharray', `${s} 60`));
    const rot = (sel, deg) => all(sel).forEach((n) => n.setAttribute('transform', `rotate(${deg} 50 50)`));
    rot('[data-kp-h]', ((d.getHours() % 12) + d.getMinutes() / 60) * 30); rot('[data-kp-m]', d.getMinutes() * 6); rot('[data-kp-s]', s * 6);
    all('[data-kp-flip]').forEach((n) => { n.innerHTML = `<b>${h[0]}</b><b>${h[1]}</b><i>:</i><b>${m[0]}</b><b>${m[1]}</b>`; });
    // o kira-start-v2.js continua buscando o estado real e escrevendo no HUD escondido: lemos de lá
    const link = document.getElementById('ks2-link')?.textContent || '—'; const key = document.getElementById('ks2-key')?.textContent || '—';
    all('[data-kp-link]').forEach((n) => { n.textContent = link; }); all('[data-kp-key]').forEach((n) => { n.textContent = key; });
    const frac = /(\d+)\s*\/\s*(\d+)/.exec(key); all('[data-kp-keybar]').forEach((n) => n.style.setProperty('--v', frac ? `${(frac[1] / frac[2]) * 100}%` : '25%'));
    all('[data-kp-voicebar]').forEach((n) => n.style.setProperty('--v', sysOk('livekit') ? '100%' : '30%'));
    all('[data-kp-st]').forEach((n) => n.classList.toggle('ok', sysOk(n.dataset.kpSt)));
    check();
  }

  let timer = 0;
  function start() {
    if (timer) return;
    build(); tick();
    timer = setInterval(tick, 1000);
    if (!frame) frame = requestAnimationFrame(draw);
  }
  function stop() {
    clearInterval(timer); timer = 0;
    if (frame) cancelAnimationFrame(frame); frame = 0;
  }
  const sync = () => (visible() ? start() : stop());
  addEventListener('resize', () => { if (visible()) build(); });
  setInterval(sync, 1500);
  document.addEventListener('visibilitychange', () => (document.hidden ? stop() : sync()));
  sync();
})();
