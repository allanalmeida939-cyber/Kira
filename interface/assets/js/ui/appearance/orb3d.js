/* Orbe 3D REAL (não é canvas 2D inclinado): uma esfera de verdade em WebGL/Three.js, com grade de meridianos e
   paralelos, que gira em todos os eixos ao arrastar — como o globo do Olho de Deus. Monta em #kira-3d-canvas.
   Lê globalThis.kiraOrbView (yaw/pitch, escrito por orbSpin.js no arraste) e soma um giro lento parado.
   O laço de quadros só roda quando o orbe "globo" está escolhido e visível: parado não custa nada.
   Desenho na placa de vídeo (WebGL); a física do arraste é barata e fica no processador. */
(function () {
  const CONTAINER_ID = 'kira-orb3d-canvas'; // container próprio (não o da galáxia)
  const COLORS = {
    roxo: 0xa855f7, ciano: 0x22d3ee, verde: 0x34d399, magenta: 0xec4899, azul: 0x60a5fa, ambar: 0xfbbf24,
  };
  const STATE_TINT = { listening: 0x6ee7ff, thinking: 0xc084fc, speaking: null, error: 0xf87171, idle: null };

  let THREE, renderer, scene, camera, group, core, grid, halo, points, raf = 0, last = 0;
  let spin = 0, started = false, container = null;

  function accentHex() {
    const a = globalThis.kiraAppearancePrefs?.accent || document.documentElement.dataset.kiraAccent || 'roxo';
    return COLORS[a] || COLORS.roxo;
  }

  function ensureContainer() {
    container = document.getElementById(CONTAINER_ID);
    if (container) return container;
    const wrapper = document.querySelector('.orb-wrapper');
    if (!wrapper) return null;
    container = document.createElement('div');
    container.id = CONTAINER_ID;
    Object.assign(container.style, { position: 'absolute', inset: '0', display: 'none', zIndex: '2' });
    wrapper.appendChild(container);
    return container;
  }

  function build() {
    container = ensureContainer();
    if (!container || !globalThis.THREE) return false;
    THREE = globalThis.THREE;
    const w = container.clientWidth || 380;
    const h = container.clientHeight || 380;
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    renderer.setPixelRatio(Math.min(2, globalThis.devicePixelRatio || 1));
    renderer.setSize(w, h, false);
    container.appendChild(renderer.domElement);
    Object.assign(renderer.domElement.style, { width: '100%', height: '100%', display: 'block' });

    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(42, w / h, 0.05, 50);
    camera.position.z = 3.1;
    group = new THREE.Group();
    scene.add(group);

    const hex = accentHex();
    // núcleo: esfera com gradiente de energia (textura procedural), para a rotação se ler como 3D
    const tex = surfaceTexture(hex);
    core = new THREE.Mesh(
      new THREE.SphereGeometry(1, 64, 48),
      new THREE.MeshBasicMaterial({ map: tex }),
    );
    group.add(core);

    // grade de meridianos/paralelos: é o que faz o olho enxergar a esfera girando em 3D
    grid = new THREE.LineSegments(gridGeometry(THREE, 1.002), new THREE.LineBasicMaterial({ color: hex, transparent: true, opacity: 0.3 }));
    group.add(grid);

    // halo de borda (fresnel simples): casca por dentro, aditiva
    halo = new THREE.Mesh(
      new THREE.SphereGeometry(1.06, 48, 32),
      new THREE.MeshBasicMaterial({ color: hex, transparent: true, opacity: 0.14, side: THREE.BackSide, blending: THREE.AdditiveBlending }),
    );
    group.add(halo);

    // poeira de pontos ao redor (profundidade)
    const n = 220, pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i += 1) {
      const r = 1.25 + Math.random() * 0.9, t = Math.random() * Math.PI * 2, p = Math.acos(2 * Math.random() - 1);
      pos[i * 3] = r * Math.sin(p) * Math.cos(t); pos[i * 3 + 1] = r * Math.sin(p) * Math.sin(t); pos[i * 3 + 2] = r * Math.cos(p);
    }
    const pg = new THREE.BufferGeometry(); pg.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
    points = new THREE.Points(pg, new THREE.PointsMaterial({ color: hex, size: 0.03, transparent: true, opacity: 0.6, depthWrite: false, blending: THREE.AdditiveBlending }));
    scene.add(points);

    if (typeof ResizeObserver === 'function') new ResizeObserver(resize).observe(container);
    return true;
  }

  function surfaceTexture(hex) {
    const c = document.createElement('canvas'); c.width = c.height = 256;
    const g = c.getContext('2d');
    const col = `#${hex.toString(16).padStart(6, '0')}`;
    g.fillStyle = '#0a0618'; g.fillRect(0, 0, 256, 256);
    const grad = g.createRadialGradient(96, 80, 10, 128, 128, 180);
    grad.addColorStop(0, col); grad.addColorStop(0.45, 'rgba(124,58,237,.55)'); grad.addColorStop(1, 'rgba(8,4,20,0)');
    g.fillStyle = grad; g.fillRect(0, 0, 256, 256);
    for (let i = 0; i < 400; i += 1) { // textura fina para a rotação não parecer lisa demais
      g.fillStyle = `rgba(200,180,255,${Math.random() * 0.06})`;
      g.fillRect(Math.random() * 256, Math.random() * 256, 1, 1);
    }
    const t = new THREE.CanvasTexture(c); t.needsUpdate = true; return t;
  }

  function gridGeometry(THREE, r) {
    const seg = [];
    for (let lat = -75; lat <= 75; lat += 15) { // paralelos
      const y = Math.sin((lat * Math.PI) / 180) * r, rad = Math.cos((lat * Math.PI) / 180) * r;
      for (let a = 0; a < 360; a += 6) {
        const a2 = a + 6;
        seg.push(rad * Math.cos((a * Math.PI) / 180), y, rad * Math.sin((a * Math.PI) / 180),
          rad * Math.cos((a2 * Math.PI) / 180), y, rad * Math.sin((a2 * Math.PI) / 180));
      }
    }
    for (let lon = 0; lon < 360; lon += 15) { // meridianos
      for (let p = -90; p < 90; p += 6) {
        const p2 = p + 6;
        const f = (pp) => [Math.cos((pp * Math.PI) / 180) * Math.cos((lon * Math.PI) / 180) * r, Math.sin((pp * Math.PI) / 180) * r, Math.cos((pp * Math.PI) / 180) * Math.sin((lon * Math.PI) / 180) * r];
        seg.push(...f(p), ...f(p2));
      }
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(seg, 3));
    return geo;
  }

  function resize() {
    if (!renderer || !container) return;
    const w = container.clientWidth || 380, h = container.clientHeight || 380;
    renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
  }

  function recolor() {
    if (!core) return;
    const state = globalThis.kiraVoiceState || 'idle';
    const tint = STATE_TINT[state];
    const hex = tint || accentHex();
    grid.material.color.setHex(hex); halo.material.color.setHex(hex); points.material.color.setHex(hex);
    core.material.map = surfaceTexture(accentHex()); core.material.needsUpdate = true;
  }
  let lastColorKey = '';

  function frame(now) {
    raf = 0;
    if (!active()) { return; }
    const dt = Math.min(0.05, (now - (last || now)) / 1000); last = now;
    const view = globalThis.kiraOrbView || { yaw: 0, pitch: 0, moving: false };
    const state = globalThis.kiraVoiceState || 'idle';
    const colorKey = `${state}:${accentHex()}`;
    if (colorKey !== lastColorKey) { lastColorKey = colorKey; recolor(); }
    // giro parado lento + o arraste (yaw/pitch); pensando/falando gira um pouco mais rápido
    const idleSpin = state === 'thinking' ? 0.5 : state === 'speaking' ? 0.3 : 0.12;
    spin += idleSpin * dt;
    group.rotation.y = spin + view.yaw;
    group.rotation.x = Math.max(-1.4, Math.min(1.4, view.pitch));
    points.rotation.y = spin * 0.4 + view.yaw * 0.5;
    // pulso com a voz
    const lvl = state === 'speaking' ? Math.min(1, (Number(globalThis.kiraVoiceLevel) || 0) / 60) : 0;
    const s = 1 + lvl * 0.12 + (state === 'listening' ? 0.03 * Math.sin(now / 350) : 0);
    group.scale.setScalar(s);
    halo.material.opacity = 0.12 + lvl * 0.25;
    renderer.render(scene, camera);
    raf = requestAnimationFrame(frame);
  }

  function active() {
    const orb = document.documentElement.dataset.kiraOrb;
    return started && orb === 'globo' && !document.hidden && container?.offsetParent !== null;
  }
  function kick() { if (active() && !raf) { last = 0; raf = requestAnimationFrame(frame); } }

  function setMode() {
    const on = document.documentElement.dataset.kiraOrb === 'globo';
    const canvas2d = document.getElementById('kira-session-orb-canvas');
    if (on && !started) started = build();
    if (container) { container.style.display = on ? 'block' : 'none'; container.setAttribute('aria-hidden', on ? 'false' : 'true'); }
    if (canvas2d) canvas2d.style.visibility = on ? 'hidden' : '';
    if (on && started) { resize(); kick(); }
  }

  function boot() {
    if (!globalThis.THREE) { setTimeout(boot, 200); return; }
    setMode();
    new MutationObserver(setMode).observe(document.documentElement, { attributes: true, attributeFilter: ['data-kira-orb'] });
    document.addEventListener('visibilitychange', kick);
    document.addEventListener('pointermove', (e) => { if (active() && e.target.closest?.('.orb-wrapper')) kick(); }, { passive: true });
    setInterval(() => { if (active() && !raf) kick(); }, 1000); // garante o giro parado e o pulso de escuta
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true }); else boot();
})();
