/* Tela de início nova: relógio e HUD, estado real dos sistemas (/api/system-status) e a rede de luz do fundo.
   Tudo para quando a tela de início some (a sessão abriu), para não gastar processador. */
(function () {
  const screen = document.getElementById('kira-start-screen');
  const welcome = document.getElementById('welcome-screen');
  if (!screen) return;
  const $ = (id) => document.getElementById(id);
  // a tela de início é fixa (offsetParent sempre nulo): vale ter caixa na página e não estar escondida
  const visible = () => !document.hidden && Boolean(welcome) && welcome.getClientRects().length > 0 && getComputedStyle(welcome).visibility !== 'hidden' && getComputedStyle(welcome).opacity !== '0';

  const voice = screen.querySelector('.ks2-voice');
  for (let i = 0; voice && i < 18; i += 1) {
    const bar = document.createElement('i');
    bar.style.animationDelay = `${(i * 73) % 900}ms`;
    voice.appendChild(bar);
  }

  const pad = (n) => String(n).padStart(2, '0');
  const DAYS = ['DOMINGO', 'SEGUNDA', 'TERÇA', 'QUARTA', 'QUINTA', 'SEXTA', 'SÁBADO'];
  function tick() {
    const d = new Date();
    $('ks2-clock').textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    $('ks2-date').textContent = `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} · ${DAYS[d.getDay()]}`;
  }

  // estado real: voz (LiveKit nuvem/local), Gemini e WhatsApp
  function row(key, label, color) {
    const node = screen.querySelector(`.ks2-sys[data-k="${key}"]`);
    if (!node) return;
    node.lastElementChild.textContent = String(label || '—').toLowerCase();
    node.classList.toggle('ok', color === 'green');
    node.classList.toggle('bad', color === 'red');
  }
  async function status() {
    try {
      const data = await (await fetch('/api/system-status', { cache: 'no-store' })).json();
      row('livekit', data.livekit?.label, data.livekit?.color);
      row('brain', data.brain?.label, data.brain?.color);
      row('whatsapp', data.whatsapp?.label, data.whatsapp?.color);
      $('ks2-link').textContent = String(data.livekit?.label || '—').toUpperCase();
      $('ks2-key').textContent = String(data.api?.label || '—').toUpperCase();
      $('ks2-health').textContent = String(data.health?.label || '—').toUpperCase();
    } catch (_) {
      /* backend ainda subindo: mantém "verificando" */
    }
  }

  // rede de pontos de luz ligados (leve: no máximo ~110 pontos, 30 quadros por segundo)
  const canvas = $('ks2-field');
  const ctx = canvas && canvas.getContext('2d');
  // o Windows do Chefe está com "mostrar animações" desligado (reduced-motion), mas ele quer a rede de luz andando
  const still = false;
  let w = 0, h = 0, pts = [], last = 0, frame = 0;
  function size() {
    if (!ctx) return;
    const dpr = Math.min(devicePixelRatio || 1, 1.5);
    w = canvas.clientWidth; h = canvas.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const n = Math.round(Math.min(110, (w * h) / 14000));
    pts = Array.from({ length: n }, () => ({ x: Math.random() * w, y: Math.random() * h, vx: (Math.random() - 0.5) * 0.25,
      vy: (Math.random() - 0.5) * 0.25, r: Math.random() * 1.6 + 0.4, hue: Math.random() < 0.25 ? 170 : 280 }));
  }
  function draw(now) {
    frame = 0;
    if (!visible()) return;
    // tela Portal (kira-portal.js) tem fundo próprio e esconde este canvas: desenhar aqui era CPU jogada fora
    if (document.documentElement.dataset.kpOn) return;
    if (now - last >= 33) {
      last = now;
      ctx.clearRect(0, 0, w, h);
      for (const p of pts) {
        if (still) continue;
        p.x += p.vx; p.y += p.vy;
        if (p.x < 0 || p.x > w) p.vx *= -1;
        if (p.y < 0 || p.y > h) p.vy *= -1;
      }
      ctx.lineWidth = 1;
      for (let i = 0; i < pts.length; i += 1) {
        for (let j = i + 1; j < pts.length; j += 1) {
          const a = pts[i], b = pts[j], d = Math.hypot(a.x - b.x, a.y - b.y);
          if (d < 130) { ctx.strokeStyle = `rgba(168,85,247,${(1 - d / 130) * 0.22})`; ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); }
        }
      }
      for (const p of pts) {
        ctx.fillStyle = `hsla(${p.hue},90%,75%,0.85)`;
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r * 1.6, 0, Math.PI * 2); ctx.globalAlpha = 0.25; ctx.fill();
        ctx.globalAlpha = 1; ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill();
      }
    }
    if (!still) frame = requestAnimationFrame(draw);
  }

  let timers = [];
  function start() {
    if (timers.length) return;
    tick(); status(); size();
    timers = [setInterval(tick, 1000), setInterval(status, 3000)];
    if (ctx && !frame) frame = requestAnimationFrame(draw);
  }
  function stop() {
    timers.forEach(clearInterval); timers = [];
    if (frame) cancelAnimationFrame(frame); frame = 0;
  }
  const sync = () => (visible() ? start() : stop());
  addEventListener('resize', () => { if (visible()) size(); });
  // a sessão some com a tela de vários jeitos (classe no body, estilo, remoção): confere de tempos em tempos
  setInterval(sync, 1500);
  document.addEventListener('visibilitychange', () => (document.hidden ? stop() : sync()));
  sync();
})();
