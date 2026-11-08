/* Olho de Deus na TV (2026-11-06). Versão leve para o navegador da LG: mapa plano (foto noturna da Terra) + pontos
   num canvas 2D, sem WebGL (o globo Three.js do PC pesa demais para a TV). Dados por /gods/<camada> (o servidor da TV
   repassa só-leitura do backend da Kira). Abre/fecha com o evento {"t":"godseye","open":true|false,camadas,lat,lon}.
   ES2017, sem ?. nem ??; só transform/opacity nas transições. Texto sempre por textContent. */
(function () {
  'use strict';
  var W = 1600, H = 800;
  var LOOK = {
    terremotos: { cor: '#fb7185', nome: 'Terremotos' },
    avioes: { cor: '#facc15', nome: 'Aviões perto de você' },
    eventos: { cor: '#fb923c', nome: 'Eventos naturais' },
    raios: { cor: '#e0f2fe', nome: 'Raios' }
  };
  var box = null, ctx = null, data = {}, opts = null, timers = [];

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }
  function xy(lat, lon) { return [(Number(lon) + 180) / 360 * W, (90 - Number(lat)) / 180 * H]; }

  function draw() {
    if (!ctx) return;
    ctx.clearRect(0, 0, W, H);
    Object.keys(data).forEach(function (key) {
      var look = LOOK[key];
      (data[key] || []).forEach(function (it) {
        if (typeof it.lat !== 'number' || typeof it.lon !== 'number') return;
        var p = xy(it.lat, it.lon);
        ctx.fillStyle = look.cor;
        ctx.globalAlpha = 0.9;
        ctx.beginPath();
        if (key === 'terremotos') ctx.arc(p[0], p[1], 3 + Math.max(0, Number(it.mag) || 0) * 2.2, 0, 6.283);
        else if (key === 'avioes') { ctx.moveTo(p[0], p[1] - 7); ctx.lineTo(p[0] + 6, p[1] + 6); ctx.lineTo(p[0] - 6, p[1] + 6); }
        else ctx.arc(p[0], p[1], key === 'raios' ? 3 : 6, 0, 6.283);
        ctx.fill();
      });
    });
    ctx.globalAlpha = 1;
    legend();
  }

  function legend() {
    var list = box && box.querySelector('.tvge-legend');
    if (!list) return;
    list.textContent = '';
    Object.keys(data).forEach(function (key) {
      var row = el('li', '', LOOK[key].nome + ': ' + (data[key] || []).length);
      row.style.borderLeftColor = LOOK[key].cor;
      list.appendChild(row);
    });
    var quakes = (data.terremotos || []).slice().sort(function (a, b) { return (b.mag || 0) - (a.mag || 0); }).slice(0, 4);
    var top = box.querySelector('.tvge-top');
    top.textContent = '';
    quakes.forEach(function (q) { top.appendChild(el('li', '', 'M' + Number(q.mag || 0).toFixed(1) + ' · ' + (q.place || ''))); });
  }

  function load(key) {
    var q = key === 'avioes' ? '?lat=' + opts.lat + '&lon=' + opts.lon + '&radius_km=300' : '';
    fetch('/gods/' + key + q).then(function (r) { return r.ok ? r.json() : { items: [] }; })
      .then(function (j) { data[key] = (j && j.items) || []; draw(); })
      .catch(function () { data[key] = data[key] || []; draw(); });
  }

  function open(msg) {
    close(true);
    opts = { lat: Number(msg.lat) || -3.73, lon: Number(msg.lon) || -38.52 };
    var layers = (msg.camadas || ['terremotos', 'avioes', 'eventos']).filter(function (k) { return LOOK[k]; });
    box = el('section', 'tvge');
    box.id = 'tv-content-godseye';
    var head = el('header', 'tvge-head');
    head.appendChild(el('h1', '', 'OLHO DE DEUS'));
    head.appendChild(el('span', 'tvge-live', 'AO VIVO'));
    var map = el('div', 'tvge-map');
    var img = el('img', 'tvge-earth');
    img.src = '/static/earth-night.jpg';
    img.alt = '';
    var canvas = el('canvas', 'tvge-dots');
    canvas.width = W; canvas.height = H;
    ctx = canvas.getContext('2d');
    var me = xy(opts.lat, opts.lon), pin = el('i', 'tvge-me');
    pin.style.left = (me[0] / W * 100) + '%'; pin.style.top = (me[1] / H * 100) + '%';
    map.appendChild(img); map.appendChild(canvas); map.appendChild(pin);
    var side = el('aside', 'tvge-side');
    side.appendChild(el('ul', 'tvge-legend'));
    side.appendChild(el('h2', '', 'Maiores terremotos'));
    side.appendChild(el('ul', 'tvge-top'));
    box.appendChild(head); box.appendChild(map); box.appendChild(side);
    document.body.appendChild(box);
    document.body.classList.add('tv-has-content');
    if (window.KiraTV && typeof window.KiraTV.contentOpen === 'function') window.KiraTV.contentOpen(box);
    requestAnimationFrame(function () { box.classList.add('is-in'); });
    layers.forEach(function (k) { data[k] = []; load(k); timers.push(setInterval(function () { load(k); }, k === 'avioes' ? 20000 : 60000)); });
  }

  function close(quiet) {
    timers.forEach(clearInterval); timers = [];
    if (!box) return;
    var old = box; box = null; ctx = null; data = {};
    if (window.KiraTV && typeof window.KiraTV.contentClose === 'function') window.KiraTV.contentClose(old);
    if (!document.querySelector('.tv-has-content-keep')) document.body.classList.remove('tv-has-content');
    old.classList.remove('is-in');
    setTimeout(function () { if (old.parentNode) old.parentNode.removeChild(old); }, quiet ? 0 : 400);
  }

  function style() {
    var css = '.tvge{position:fixed;left:5%;top:7%;width:90%;height:86%;z-index:50;display:grid;grid-template-columns:1fr 22%;' +
      'grid-template-rows:auto 1fr;gap:24px;color:#f5f3ff;font-family:Segoe UI,Arial,sans-serif;opacity:0;' +
      'transform:translateY(40px) scale(.96);transition:opacity .4s ease,transform .4s ease}' +
      '.tvge.is-in{opacity:1;transform:none}.tvge-head{grid-column:1/3;display:flex;align-items:center;gap:24px}' +
      '.tvge-head h1{margin:0;font-size:44px;letter-spacing:.3em;font-weight:300}' +
      '.tvge-live{font-size:24px;padding:4px 14px;border-radius:99px;background:#be123c;letter-spacing:.15em}' +
      '.tvge-map{position:relative;border-radius:18px;overflow:hidden;background:#05030c;border:1px solid rgba(192,132,252,.35)}' +
      '.tvge-earth,.tvge-dots{position:absolute;left:0;top:0;width:100%;height:100%;object-fit:fill}' +
      '.tvge-me{position:absolute;width:22px;height:22px;margin:-11px;border-radius:50%;border:3px solid #a855f7;background:rgba(168,85,247,.35)}' +
      '.tvge-side{font-size:28px}.tvge-side ul{list-style:none;margin:0 0 24px;padding:0}' +
      '.tvge-legend li{border-left:8px solid #fff;padding:6px 0 6px 14px;margin-bottom:10px}' +
      '.tvge-side h2{font-size:28px;font-weight:600;margin:0 0 10px;color:#fda4af}' +
      '.tvge-top li{font-size:24px;line-height:1.3;margin-bottom:12px;opacity:.9}' +
      'body.tv-has-content .clock,body.tv-has-content .status{opacity:0}';
    var tag = document.createElement('style');
    tag.textContent = css;
    document.head.appendChild(tag);
  }

  function listen() {
    var es;
    try { es = new EventSource('/events'); } catch (e) { setTimeout(listen, 3000); return; }
    es.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!msg || msg.t !== 'godseye') return;
      if (msg.open) open(msg); else close(false);
    };
    es.onerror = function () { es.close(); setTimeout(listen, 3000); };
  }

  style();
  window.KiraTVGodsEye = { open: open, close: close };
  if (!/[?&]demo=1/.test(location.search)) listen();
})();
