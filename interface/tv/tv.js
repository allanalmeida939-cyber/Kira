/* Kira na TV (2026-11-06): a tela da Kira no navegador da TV LG. Script clássico ES2017 (navegador da TV é antigo):
   sem ?. ?? nem classes privadas. KiraTV.mount(stage, {modelo, demo, events}) monta um palco 1920x1080 num dos 5 modelos.
   Orbe: renderers do orbRenderersV2 (window.KiraOrbRenderers, carregado por módulo) ou um orbe simples embutido.
   Dado vindo da Kira entra sempre por textContent; imagem só http(s). */
(function () {
  'use strict';
  var TAU = Math.PI * 2;
  var MODELS = {
    1: { name: 'Cinema', orb: 'nuvem', accent: { main: [168, 85, 247], soft: [233, 213, 255], listen: [110, 231, 255] } },
    2: { name: 'Tático', orb: 'colmeia', accent: { main: [45, 212, 191], soft: [204, 251, 241], listen: [253, 224, 71] } },
    3: { name: 'Revista', orb: 'voxels', accent: { main: [190, 18, 60], soft: [80, 20, 30], listen: [29, 78, 216] } },
    4: { name: 'Galeria', orb: 'nuvem', accent: { main: [251, 191, 36], soft: [254, 243, 199], listen: [125, 211, 252] } },
    5: { name: 'Terminal', orb: 'colmeia', accent: { main: [217, 70, 239], soft: [250, 232, 255], listen: [74, 222, 128] } }
  };
  var STATE_LABEL = { idle: 'em espera', listening: 'ouvindo', thinking: 'pensando', speaking: 'falando', error: 'erro' };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null && text !== '') n.textContent = String(text);
    return n;
  }
  function str(v, max) { return v === undefined || v === null ? '' : String(v).slice(0, max || 600); }
  function arr(v, n) { return Array.isArray(v) ? v.slice(0, n || 12) : []; }
  // Imagem: a TV não alcança 127.0.0.1 nem o proxy da Kira; vai tudo pelo /img do servidor da TV (só image/*).
  var PROXY = location.protocol !== 'file:';
  function img(v) {
    var u = typeof v === 'string' ? v : (v && (v.url || v.src || v.imageUrl || v.image || v.thumbnail || v.capa)) || '';
    if (/^https?:\/\//i.test(u) || /^\/api\//.test(u)) return PROXY ? '/img?u=' + encodeURIComponent(u) : u;
    return '';
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }

  /* ---------- orbe embutido (sem módulo): nuvem de pontos simples ---------- */
  function fallbackOrb() {
    var pts = [];
    for (var i = 0; i < 500; i++) { var y = 1 - 2 * (i + 0.5) / 500, r = Math.sqrt(1 - y * y), a = i * 2.39996; pts.push([r * Math.cos(a), y, r * Math.sin(a)]); }
    return { draw: function (ctx, o) {
      var cx = o.w / 2, cy = o.h / 2, R = Math.min(o.w, o.h) * 0.32, rot = o.t * (o.state === 'thinking' ? 1 : 0.25);
      var c = o.state === 'listening' ? o.accent.listen : o.accent.main, cs = Math.cos(rot), sn = Math.sin(rot);
      ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, o.w, o.h);
      for (var j = 0; j < pts.length; j++) {
        var p = pts[j], k = 1 + (o.state === 'speaking' ? o.level * 0.3 * Math.sin(p[1] * 6 + o.t * 6) : 0);
        var x = p[0] * cs - p[2] * sn, z = p[0] * sn + p[2] * cs, d = (z + 1) / 2;
        ctx.fillStyle = 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + (0.2 + d * 0.8).toFixed(2) + ')';
        ctx.fillRect(cx + x * R * k, cy + p[1] * R * k, 1.5 + d * 2, 1.5 + d * 2);
      }
    } };
  }
  function makeOrb(kind) {
    var lib = window.KiraOrbRenderers;
    var f = lib && ({ nuvem: lib.createCloudRenderer, colmeia: lib.createGeoHiveRenderer, voxels: lib.createVoxelRenderer })[kind];
    try { return f ? f() : fallbackOrb(); } catch (e) { return fallbackOrb(); }
  }

  /* ---------- ícones desenhados (SVG, traço único) ---------- */
  var SVGNS = 'http://www.w3.org/2000/svg';
  var ICONS = {
    sun: 'M12 7a5 5 0 1 0 0 10a5 5 0 1 0 0-10M12 1v3M12 20v3M1 12h3M20 12h3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1',
    cloud: 'M7 18h10a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.2 9.6A4.2 4.2 0 0 0 7 18z',
    partly: 'M8 3v2M3.5 5.5l1.4 1.4M2 10h2M8 7a3 3 0 0 0-2.6 4.5M9 19h8a3.5 3.5 0 0 0 .5-6.96A5 5 0 0 0 8.2 11.2A3.9 3.9 0 0 0 9 19z',
    rain: 'M7 14h10a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.2 5.6A4.2 4.2 0 0 0 7 14zM8 17l-1 3M12 17l-1 3M16 17l-1 3',
    storm: 'M7 14h10a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.2 5.6A4.2 4.2 0 0 0 7 14zM12 15l-2 4h3l-2 4',
    snow: 'M12 2v20M3.3 7l17.4 10M3.3 17L20.7 7M9 4l3 2l3-2M9 20l3-2l3 2',
    fog: 'M4 9h16M2 13h20M5 17h14',
    news: 'M4 5h13v14H6a2 2 0 0 1-2-2zM17 9h3v8a2 2 0 0 1-2 2M7 9h7M7 12h7M7 15h5',
    calendar: 'M4 6h16v14H4zM4 10h16M8 3v4M16 3v4',
    mail: 'M3 6h18v12H3zM3 7l9 6l9-6',
    list: 'M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01',
    chart: 'M4 20V10M10 20V4M16 20v-7M2 20h20',
    table: 'M3 5h18v14H3zM3 10h18M3 15h18M9 5v14',
    question: 'M12 22a10 10 0 1 0 0-20a10 10 0 1 0 0 20M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2.5-3 4.5M12 17.5h.01',
    cards: 'M6 4h12v16H6zM3 7v13a1 1 0 0 0 1 1h11',
    image: 'M3 5h18v14H3zM3 16l5-5l4 4l3-3l6 6M15.5 8.5h.01',
    music: 'M9 18V5l11-2v13M9 18a3 3 0 1 1-6 0a3 3 0 0 1 6 0M20 16a3 3 0 1 1-6 0a3 3 0 0 1 6 0',
    info: 'M12 22a10 10 0 1 0 0-20a10 10 0 1 0 0 20M12 11v6M12 7.5h.01',
    book: 'M4 4h7a3 3 0 0 1 3 3v13a2 2 0 0 0-2-2H4zM20 4h-6M20 4v14h-6',
    bell: 'M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 0 0 3.4 0'
  };
  function icon(name, cls) {
    var s = document.createElementNS(SVGNS, 'svg'), p = document.createElementNS(SVGNS, 'path');
    s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('class', 'ktv-ico' + (cls ? ' ' + cls : '')); s.setAttribute('aria-hidden', 'true');
    p.setAttribute('d', ICONS[name] || ICONS.info); s.appendChild(p); return s;
  }
  // código WMO do clima -> ícone
  function wxIcon(code) {
    code = Number(code);
    if (code === 0) return 'sun';
    if (code >= 1 && code <= 3) return code === 3 ? 'cloud' : 'partly';
    if (code === 45 || code === 48) return 'fog';
    if (code >= 71 && code <= 77) return 'snow';
    if (code >= 95) return 'storm';
    if (code >= 51) return 'rain';
    return 'partly';
  }
  var DOW = ['dom', 'seg', 'ter', 'qua', 'qui', 'sex', 'sáb'];
  function dayName(iso, i) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(str(iso));
    if (!m) return '';
    return i === 0 ? 'hoje' : DOW[new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).getDay()];
  }
  function when(v) {
    if (!v) return '';
    var d = typeof v === 'number' ? new Date(v < 1e12 ? v * 1000 : v) : new Date(String(v));
    if (isNaN(d.getTime())) return '';
    var mins = Math.round((Date.now() - d.getTime()) / 60000);
    if (mins >= 0 && mins < 60) return 'há ' + Math.max(1, mins) + ' min';
    if (mins >= 60 && mins < 1440) return 'há ' + Math.round(mins / 60) + ' h';
    return pad(d.getDate()) + '/' + pad(d.getMonth() + 1);
  }
  function deg(v) { return v === null || v === undefined || v === '' || isNaN(Number(v)) ? '–' : Math.round(Number(v)) + '°'; }
  function has(v) { return v !== null && v !== undefined && v !== ''; }

  /* ---------- conteúdo: ação da Kira -> {cat, ic, title, meta, pages:[{head, blocks}]} ---------- */
  var QUADRO_LOOK = { info: ['Resumo', 'info'], lista: ['Lista', 'list'], grafico: ['Gráfico', 'chart'], questoes: ['Questões', 'question'], noticia: ['Notícia', 'news'], tabela: ['Tabela', 'table'], flashcards: ['Flashcards', 'cards'] };
  var SECTION_LOOK = { weather: ['Clima', 'sun'], news: ['Notícias', 'news'], agenda: ['Agenda', 'calendar'], email: ['E-mails', 'mail'], image_gallery: ['Imagens', 'image'], chart: ['Gráfico', 'chart'], timeline: ['Linha do tempo', 'list'], text: ['Resumo', 'info'], election: ['Eleição', 'chart'], briefing: ['Briefing do dia', 'bell'] };

  function sectionBlocks(s) {
    var d = s.data || {}, b = [];
    if (s.status === 'unavailable') return [{ t: 'note', v: str(s.reason || 'Indisponível agora.', 200) }];
    if (s.status === 'pending' && !s.data) return [{ t: 'note', v: 'Carregando…' }];
    if (d.current) {
      var c = d.current, loc = d.location || {}, today = arr(d.forecast, 1)[0] || {};
      b.push({ t: 'weather', loc: [str(loc.name, 60), str(loc.admin1 || loc.region || loc.state, 40)].filter(Boolean).join(', '),
        temp: deg(c.temperature), cond: str(c.condition, 60), ic: wxIcon(c.code),
        stats: [['Sensação', deg(c.apparentTemperature)], ['Umidade', has(c.humidity) ? Math.round(c.humidity) + '%' : '–'],
          ['Vento', has(c.windSpeed) ? Math.round(c.windSpeed) + ' km/h' : '–'], ['Chuva', has(today.precipitationProbability) ? today.precipitationProbability + '%' : '–']],
        days: arr(d.forecast, 6).map(function (f, i) { return { d: dayName(f.date, i), ic: wxIcon(f.code), min: deg(f.min), max: deg(f.max), rain: f.precipitationProbability }; }) });
    }
    var arts = arr(d.articles || (s.type === 'news' ? d.items : null), 6).filter(function (a) { return a && typeof a === 'object'; });
    if (arts.length) b.push({ t: 'news', v: arts.map(function (a) { return { img: img(a.imageUrl || a.image || a.thumbnail), title: str(a.title, 180), src: str(a.publisher || a.source, 50), when: when(a.publishedAt || a.ts), sum: str(a.summary, 220) }; }) });
    if (arr(d.events).length) b.push({ t: 'agenda', v: arr(d.events, 8).map(function (e) { return typeof e === 'string' ? { h: '', v: e } : { h: str(e.startAt || e.time || e.hora || e.start, 25).replace(/^.*T(\d\d:\d\d).*$/, '$1'), v: str(e.title || e.titulo || e.summary, 140), warn: !!(e.conflictsWith && e.conflictsWith.length) }; }) });
    if (arr(d.messages).length) b.push({ t: 'mail', v: arr(d.messages, 6).map(function (m) { return typeof m === 'string' ? { from: '', subj: m } : { from: str(m.from || m.de || m.sender, 50), subj: str(m.subject || m.assunto || m.text, 140), hi: m.priority === 'high' || m.prioridade === 'alta' }; }) });
    var pics = arr(d.images || (s.type === 'image_gallery' ? d.items : null), 6).map(function (x) { return { u: img(x), c: str(x && (x.caption || x.credit || x.title), 120) }; }).filter(function (x) { return x.u; });
    if (pics.length) b.push({ t: 'mosaic', v: pics });
    else if (img(d.image || d.imagem || d.imageUrl)) b.push({ t: 'hero', v: img(d.image || d.imagem || d.imageUrl), c: str(d.caption || d.credit, 120) });
    if (arr(d.points).length) b.push({ t: 'points', v: arr(d.points, 8).map(function (x) { return str(x, 220); }) });
    arr(d.paragraphs, 6).forEach(function (x) { b.push({ t: 'text', v: str(x, 900) }); });
    var txt = d.text || d.summary || d.resumo || d.description;
    if (txt) b.push({ t: 'text', v: str(txt, 900) });
    var labels = arr(d.labels, 12), values = arr(d.values || d.data, 12);
    if (labels.length && values.length) b.push({ t: 'bars', v: labels.map(function (l, i) { return { r: str(l, 24), v: Number(values[i]) || 0 }; }) });
    var items = s.type === 'news' || s.type === 'image_gallery' ? [] : arr(d.items, 8).filter(Boolean);
    if (items.length && !b.length) b.push({ t: 'points', v: items.map(function (it) { return typeof it === 'string' ? it : str(it.title || it.titulo || it.name || it.label || it.text) + (it.summary ? ' — ' + str(it.summary, 160) : ''); }) });
    if (!b.length) b.push({ t: 'note', v: 'Sem dados nesta parte.' });
    return b;
  }

  function contentOf(action) {
    var p = action.props || {}, c = { title: str(action.title || p.titulo || p.title, 120), cat: 'Kira', ic: 'info', meta: '', tipo: action.kind || action.type, pages: [] };
    var one = [];
    function page(head, blocks) { if (blocks.length) c.pages.push({ head: head, blocks: blocks }); }
    if (action.type === 'present') {
      c.tipo = 'present';
      var secs = arr(action.sections, 12).filter(function (s) { return s && s.type !== 'sources'; });
      var look = SECTION_LOOK[secs.length > 1 ? 'briefing' : (secs[0] && secs[0].type) || 'text'] || SECTION_LOOK.text;
      c.cat = look[0]; c.ic = look[1];
      secs.forEach(function (s) { var lk = SECTION_LOOK[s.type] || ['', 'info']; page({ v: str(s.heading || lk[0], 120), ic: lk[1] }, sectionBlocks(s)); });
      c.meta = arr(action.sources, 4).map(function (x) { return str(x && x.name, 40); }).filter(Boolean).join(' · ');
      return c;
    }
    if (action.kind === 'quadro') {
      var tipo = p.tipo || 'info', lk2 = QUADRO_LOOK[tipo] || ['Kira', 'info'];
      c.tipo = tipo; c.cat = lk2[0]; c.ic = lk2[1]; c.meta = str(p.fonte, 60);
      if (tipo === 'noticia' && img(p.imagem)) one.push({ t: 'hero', v: img(p.imagem), c: str(p.fonte, 80) });
      if (p.subtitulo) one.push({ t: 'lead', v: str(p.subtitulo, 300) });
      if (p.resumo) one.push({ t: 'text', v: str(p.resumo, 600) });
      if (tipo === 'lista') one.push({ t: p.numerada ? 'steps' : 'points', v: arr(p.itens, 10).map(function (x) { return str(x, 220); }) });
      if (tipo === 'info' && arr(p.itens).length) one.push({ t: 'facts', v: arr(p.itens, 8).map(function (it) { return [str(it && it.rotulo, 60), str(it && it.valor, 80)]; }) });
      if (tipo === 'grafico') one.push({ t: 'bars', v: arr(p.dados, 10).map(function (d) { return { r: str(d && (d.rotulo || d.label), 30), v: Number(d && (d.valor !== undefined ? d.valor : d.value)) || 0 }; }) });
      if (tipo === 'tabela') one.push({ t: 'table', cols: arr(p.colunas, 6).map(function (x) { return str(x, 40); }), rows: arr(p.linhas, 10).map(function (r) { return arr(r, 6).map(function (x) { return str(x, 80); }); }) });
      if (tipo === 'noticia') arr(p.artigo, 6).forEach(function (x) { one.push({ t: 'text', v: str(x, 900) }); });
      if (p.destaque) one.push({ t: 'quote', v: str(p.destaque, 300) });
      page(null, one);
      if (tipo === 'questoes') arr(p.questoes, 6).forEach(function (q, i) { page({ v: 'Questão ' + (i + 1), ic: 'question' }, [{ t: 'quiz', q: str(q && q.enunciado, 500), alts: arr(q && q.alternativas, 5).map(function (a) { return str(a, 180); }), right: has(q && q.resposta) ? Number(q.resposta) : -1, why: str(q && q.explicacao, 300) }]); });
      if (tipo === 'flashcards') page(null, [{ t: 'flash', v: arr(p.cards, 8).map(function (x) { return [str(x && x.frente, 200), str(x && x.verso, 300)]; }) }]);
      return c;
    }
    if (action.kind === 'tutor') {
      c.cat = 'Estudos'; c.ic = 'book';
      if (p.mode === 'quiz' && p.quiz) {
        c.title = c.title || 'Simulado'; c.meta = str(p.quiz.tema || p.quiz.curso, 60);
        arr(p.quiz.questoes, 10).forEach(function (q, i) { page({ v: 'Questão ' + (i + 1) + (q.tema ? ' · ' + str(q.tema, 40) : ''), ic: 'question' }, [{ t: 'quiz', q: str(q.enunciado, 500), alts: arr(q.alternativas, 5).map(function (a) { return str(a, 180); }), right: -1 }]); });
      } else {
        c.title = str(p.titulo, 120) || c.title;
        if (img(p.imagem)) one.push({ t: 'hero', v: img(p.imagem), c: str(p.imagem && p.imagem.credito, 120) });
        str(p.texto, 3000).split(/\n+/).forEach(function (ln) {
          if (/^- \[[ x]\]/i.test(ln)) one.push({ t: 'check', v: ln.replace(/^- \[[ x]\]\s*/i, ''), done: /^- \[x\]/i.test(ln) });
          else if (ln.trim()) one.push({ t: 'text', v: ln });
        });
        page(null, one);
      }
      return c;
    }
    if (action.kind === 'viewer' || action.kind === 'gallery' || action.kind === 'creation') {
      c.cat = action.kind === 'creation' ? 'Criação' : 'Imagens'; c.ic = 'image'; c.tipo = 'media';
      var list = [];
      var main = img(p.imageUrl || p.url || p.src || p.image || p.imagem || (p.item && (p.item.url || p.item.imageUrl)));
      if (main) list.push({ u: main, c: str(p.credit || p.source || p.caption || (p.item && (p.item.credit || p.item.title)), 140) });
      arr(p.items, 6).forEach(function (it) { var u = img(it); if (u && u !== main) list.push({ u: u, c: str(it && (it.credit || it.source || it.title), 120) }); });
      if (list.length === 1) one.push({ t: 'hero', v: list[0].u, c: list[0].c, full: true }); else if (list.length) one.push({ t: 'mosaic', v: list });
      if (p.caption || p.description || p.prompt) one.push({ t: 'lead', v: str(p.caption || p.description || p.prompt, 300) });
      c.meta = str(p.query || p.topic, 60);
      page(null, one);
      return c;
    }
    if (img(p.image || p.thumbnail || p.imagem)) one.push({ t: 'hero', v: img(p.image || p.thumbnail || p.imagem), c: '' });
    var body = p.summary || p.text || p.content || p.body || p.description || p.resumo;
    if (body) one.push({ t: 'text', v: str(body, 1200) });
    var its = arr(p.items, 8).map(function (it) { return typeof it === 'string' ? it : str(it && (it.title || it.name || it.label), 180); }).filter(Boolean);
    if (its.length) one.push({ t: 'points', v: its });
    page(null, one);
    c.meta = str(p.query || p.topic || p.source, 60);
    return c;
  }

  /* ---------- desenho dos blocos (sempre textContent) ---------- */
  function picture(src, caption, cls) {
    var fig = el('figure', 'ktv-fig ' + (cls || '')), im = el('img'); im.alt = ''; im.decoding = 'async';
    im.onerror = function () { fig.classList.add('is-broken'); };
    im.src = src; fig.appendChild(im);
    if (caption) fig.appendChild(el('figcaption', '', caption));
    return fig;
  }
  function block(b) {
    var n, row;
    switch (b.t) {
      case 'text': return el('p', 'ktv-text', b.v);
      case 'lead': return el('p', 'ktv-lead', b.v);
      case 'note': return el('p', 'ktv-note', b.v);
      case 'quote': return el('blockquote', 'ktv-quote', b.v);
      case 'hero': return picture(b.v, b.c, 'is-hero' + (b.full ? ' is-full' : ''));
      case 'mosaic':
        n = el('div', 'ktv-mosaic n' + Math.min(b.v.length, 4));
        b.v.slice(0, 4).forEach(function (x) { n.appendChild(picture(x.u, x.c)); });
        return n;
      case 'points': case 'steps':
        n = el(b.t === 'steps' ? 'ol' : 'ul', 'ktv-points' + (b.t === 'steps' ? ' is-steps' : ''));
        b.v.forEach(function (x) { n.appendChild(el('li', '', x)); });
        return n;
      case 'check':
        n = el('div', 'ktv-check' + (b.done ? ' is-done' : '')); n.appendChild(el('i')); n.appendChild(el('span', '', b.v)); return n;
      case 'facts':
        n = el('div', 'ktv-facts');
        b.v.forEach(function (f) { row = el('div', 'ktv-fact'); row.appendChild(el('strong', '', f[1])); row.appendChild(el('span', '', f[0])); n.appendChild(row); });
        return n;
      case 'bars':
        var max = 1; b.v.forEach(function (d) { max = Math.max(max, Math.abs(d.v)); });
        n = el('div', 'ktv-bars');
        b.v.forEach(function (d) {
          row = el('div', 'ktv-bar'); var track = el('b'), fill = el('i');
          fill.style.transform = 'scaleX(' + Math.max(0.02, Math.abs(d.v) / max).toFixed(3) + ')';
          track.appendChild(fill); row.appendChild(el('span', '', d.r)); row.appendChild(track);
          row.appendChild(el('em', '', String(d.v).replace('.', ','))); n.appendChild(row);
        });
        return n;
      case 'table':
        n = el('table', 'ktv-table'); row = el('tr');
        b.cols.forEach(function (x) { row.appendChild(el('th', '', x)); }); n.appendChild(row);
        b.rows.forEach(function (r) { var tr = el('tr'); r.forEach(function (x) { tr.appendChild(el('td', '', x)); }); n.appendChild(tr); });
        return n;
      case 'quiz':
        n = el('div', 'ktv-quiz'); n.appendChild(el('p', 'ktv-q', b.q));
        b.alts.forEach(function (a, k) {
          var alt = el('div', 'ktv-alt' + (b.right === k ? ' is-right' : ''));
          alt.appendChild(el('b', '', 'ABCDE'.charAt(k))); alt.appendChild(el('span', '', a.replace(/^[A-Ea-e]\)\s*/, ''))); n.appendChild(alt);
        });
        if (b.why) n.appendChild(el('p', 'ktv-why', b.why));
        return n;
      case 'flash':
        n = el('div', 'ktv-flashes');
        b.v.forEach(function (f) { var cd = el('div', 'ktv-flash'); cd.appendChild(el('p', 'ktv-front', f[0])); cd.appendChild(el('p', 'ktv-back', f[1])); n.appendChild(cd); });
        return n;
      case 'weather':
        n = el('div', 'ktv-wx');
        var now = el('div', 'ktv-wx-now'); now.appendChild(icon(b.ic, 'is-big'));
        var big = el('div', 'ktv-wx-temp'); big.appendChild(el('strong', '', b.temp)); big.appendChild(el('span', '', b.cond)); if (b.loc) big.appendChild(el('em', '', b.loc));
        now.appendChild(big);
        var st = el('div', 'ktv-wx-stats');
        b.stats.forEach(function (s) { var x = el('div'); x.appendChild(el('span', '', s[0])); x.appendChild(el('strong', '', s[1])); st.appendChild(x); });
        now.appendChild(st); n.appendChild(now);
        if (b.days.length) {
          var days = el('div', 'ktv-wx-days');
          b.days.forEach(function (d) {
            var x = el('div', 'ktv-wx-day'); x.appendChild(el('span', 'ktv-wx-dow', d.d)); x.appendChild(icon(d.ic));
            var mm = el('div', 'ktv-wx-mm'); mm.appendChild(el('strong', '', d.max)); mm.appendChild(el('span', '', d.min)); x.appendChild(mm);
            if (has(d.rain)) x.appendChild(el('em', '', d.rain + '% chuva'));
            days.appendChild(x);
          });
          n.appendChild(days);
        }
        return n;
      case 'news':
        n = el('div', 'ktv-news');
        b.v.forEach(function (a, k) {
          var it = el('article', 'ktv-news-item' + (k === 0 ? ' is-top' : ''));
          if (a.img) it.appendChild(picture(a.img, '', 'ktv-news-img')); else it.appendChild(el('div', 'ktv-news-img is-empty'));
          var tx = el('div', 'ktv-news-tx'); tx.appendChild(el('h4', '', a.title));
          if (k === 0 && a.sum) tx.appendChild(el('p', '', a.sum));
          tx.appendChild(el('span', 'ktv-news-meta', [a.src, a.when].filter(Boolean).join(' · ')));
          it.appendChild(tx); n.appendChild(it);
        });
        return n;
      case 'agenda':
        n = el('div', 'ktv-agenda');
        b.v.forEach(function (e) { row = el('div', 'ktv-ag' + (e.warn ? ' is-warn' : '')); row.appendChild(el('time', '', e.h || '—')); row.appendChild(el('span', '', e.v)); n.appendChild(row); });
        return n;
      case 'mail':
        n = el('div', 'ktv-mail');
        b.v.forEach(function (m) { row = el('div', 'ktv-ml' + (m.hi ? ' is-hi' : '')); row.appendChild(icon('mail')); var tx = el('div'); tx.appendChild(el('strong', '', m.subj)); if (m.from) tx.appendChild(el('span', '', m.from)); row.appendChild(tx); n.appendChild(row); });
        return n;
    }
    return el('p', 'ktv-note', '');
  }
  function renderCard(c) {
    var card = el('article', 'ktv-card t-' + c.tipo);
    var head = el('header', 'ktv-head'); head.appendChild(icon(c.ic)); head.appendChild(el('span', 'ktv-cat', c.cat));
    if (c.meta) head.appendChild(el('span', 'ktv-meta', c.meta));
    card.appendChild(head);
    if (c.title) card.appendChild(el('h2', 'ktv-title', c.title));
    var pages = el('div', 'ktv-pages');
    (c.pages.length ? c.pages : [{ head: null, blocks: [{ t: 'note', v: 'Sem conteúdo.' }] }]).forEach(function (pg, k) {
      var sec = el('section', 'ktv-page' + (k === 0 ? ' is-on' : '')), inner = el('div', 'ktv-scroll');
      // uma página só: o subtítulo repetiria a categoria/título ("Clima" em "Clima em Fortaleza")
      if (pg.head && c.pages.length > 1) { var h = el('h3', 'ktv-ph'); h.appendChild(icon(pg.head.ic)); h.appendChild(el('span', '', pg.head.v)); inner.appendChild(h); }
      pg.blocks.forEach(function (b) { inner.appendChild(block(b)); });
      sec.appendChild(inner); pages.appendChild(sec);
    });
    card.appendChild(pages);
    if (c.pages.length > 1) {
      var dots = el('div', 'ktv-dots');
      c.pages.forEach(function (pg, k) { var d = el('span', k === 0 ? 'is-on' : ''); d.appendChild(el('i')); d.appendChild(el('b', '', pg.head ? pg.head.v : String(k + 1))); dots.appendChild(d); });
      card.appendChild(dots);
    }
    return card;
  }
  // Nada cortado: página que não cabe rola sozinha (só transform); várias páginas trocam depois de ler a atual.
  function animateCard(card) {
    stopCard(card);
    var pages = card.querySelectorAll('.ktv-page'), dots = card.querySelectorAll('.ktv-dots > span'), cur = 0, timers = [];
    card._ktvTimers = timers;
    function later(fn, ms) { timers.push(setTimeout(fn, ms)); }
    function show(k) {
      for (var i = 0; i < pages.length; i++) { pages[i].classList.toggle('is-on', i === k); if (dots[i]) dots[i].classList.toggle('is-on', i === k); }
      var page = pages[k], inner = page.querySelector('.ktv-scroll');
      inner.style.transition = 'none'; inner.style.transform = 'translateY(0)';
      var over = Math.max(0, inner.scrollHeight - page.clientHeight);
      var read = 5000 + Math.min(7000, (inner.textContent || '').length * 20);
      if (over > 8) {
        var dur = Math.max(4000, over * 22); // ~45 px/s: dá para ler de longe
        later(function () { inner.style.transition = 'transform ' + dur + 'ms linear'; inner.style.transform = 'translateY(' + (-over) + 'px)'; }, 3500);
        read = 3500 + dur + 3500;
      }
      if (pages.length > 1) later(function () { cur = (cur + 1) % pages.length; show(cur); }, read);
      else if (over > 8) later(function () { show(0); }, read);
    }
    requestAnimationFrame(function () { show(0); });
  }
  function stopCard(card) { (card._ktvTimers || []).forEach(clearTimeout); card._ktvTimers = []; }

  /* ---------- montagem de um palco ---------- */
  function mount(stage, opts) {
    opts = opts || {};
    var model = MODELS[opts.modelo] ? Number(opts.modelo) : 1;
    var state = 'idle', level = 0, target = 0, orb = null, ctx = null, canvas = null, lastDraw = 0, raf = 0, panels = {}, visible = true, voiceHere = false;
    var root = el('div', 'ktv');
    stage.innerHTML = ''; stage.appendChild(root);

    function build() {
      var m = MODELS[model];
      root.className = 'ktv m' + model + ' is-' + state + (voiceHere ? ' voice-here' : '');
      root.innerHTML = '';
      root.appendChild(el('div', 'ktv-bg'));
      var orbBox = el('div', 'ktv-orb'); canvas = el('canvas'); canvas.width = canvas.height = model === 1 ? 480 : model === 3 ? 260 : 360;
      orbBox.appendChild(canvas); orbBox.appendChild(el('div', 'ktv-state'));
      var clock = el('div', 'ktv-clock'); clock.appendChild(el('div', 'ktv-time')); clock.appendChild(el('div', 'ktv-date'));
      var deck = el('div', 'ktv-deck'), music = el('div', 'ktv-music');
      root.appendChild(orbBox); root.appendChild(clock); root.appendChild(deck); root.appendChild(music);
      if (model === 2) { ['tl', 'tr', 'bl', 'br'].forEach(function (c) { root.appendChild(el('i', 'ktv-corner ' + c)); }); root.appendChild(el('div', 'ktv-ticks')); }
      if (model === 3) { var mast = el('div', 'ktv-mast'); mast.appendChild(el('span', '', 'KIRA')); mast.appendChild(el('span', 'ktv-ed', 'Edição da sala')); root.appendChild(mast); }
      if (model === 5) root.appendChild(el('div', 'ktv-prompt', 'kira@sala:~$'));
      ctx = canvas.getContext('2d'); orb = makeOrb(m.orb);
      tick(); Object.keys(panels).forEach(function (id) { place(panels[id].action, true); }); focus();
    }

    function tick() {
      var n = new Date(), t = root.querySelector('.ktv-time'), d = root.querySelector('.ktv-date');
      if (!t) return;
      t.textContent = pad(n.getHours()) + ':' + pad(n.getMinutes());
      var dias = ['domingo', 'segunda', 'terça', 'quarta', 'quinta', 'sexta', 'sábado'], meses = ['janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro'];
      d.textContent = dias[n.getDay()] + ', ' + n.getDate() + ' de ' + meses[n.getMonth()];
      var s = root.querySelector('.ktv-state'); if (s) s.textContent = model === 5 ? '[kira]' : 'Kira'; // só o nome (pedido do Chefe, 2026-11-07)
    }

    // Orbe (2026-11-07): anima de verdade só falando ou pensando (todo quadro, fluido); parado/ouvindo fica quase
    // estático: o relógio do orbe anda a 12% e desenha 8x/s. A troca de ritmo é suave (spd vai chegando ao alvo).
    var vt = 0, lastNow = 0, spd = 0.12;
    function frame(now) {
      raf = 0;
      if (!visible || !ctx) return;
      level += (target - level) * (target > level ? 0.35 : 0.08);
      var busy = state === 'speaking' || state === 'thinking' || level > 0.05;
      spd += ((busy ? 1 : 0.12) - spd) * 0.06;
      vt += (lastNow ? Math.min(0.1, (now - lastNow) / 1000) : 0) * spd; lastNow = now;
      if (spd > 0.2 || now - lastDraw >= 125) {
        lastDraw = now;
        try { orb.draw(ctx, { t: vt, w: canvas.width, h: canvas.height, state: state, level: Math.min(1, level), accent: MODELS[model].accent }); } catch (e) { orb = fallbackOrb(); }
      }
      raf = requestAnimationFrame(frame);
    }

    // Entrada saindo do orbe: começa no centro do orbe, pequena e transparente, e vai para o lugar (transform/opacity).
    function enter(node) { // só aparece (sem voar do orbe): a TV engasgava e o painel entrava clarinho e tremendo
      node.style.transition = 'none'; node.style.opacity = ''; node.style.transform = '';
    }

    // Foco (pedido do Chefe, 2026-11-06): com qualquer conteúdo na TV somem relógio e estado, o orbe encolhe para o
    // canto de cima à direita (continua reagindo à voz) e o conteúdo fica grande no centro. FLIP: só transform.
    var external = 0; // painéis do PC (tvKira.js) e Olho de Deus abertos
    function focus() {
      var on = Object.keys(panels).length > 0 || external > 0 || root.classList.contains('has-music');
      root.classList.toggle('is-focus', on);
      var o = root.querySelector('.ktv-orb'); if (!o) return;
      if (!on) { o.style.transform = ''; return; }
      var size = 168, s = size / (o.offsetWidth || size);
      o.style.transformOrigin = '0 0';
      o.style.transform = 'translate(' + (1920 - 64 - size - o.offsetLeft) + 'px,' + (52 - o.offsetTop) + 'px) scale(' + s.toFixed(3) + ')';
    }
    function place(action, silent) {
      var id = str(action.id || action.presentationId || action.kind || 'p', 64), card = renderCard(contentOf(action));
      var deck = root.querySelector('.ktv-deck');
      var old = panels[id] && panels[id].node;
      if (old) stopCard(old);
      if (old && old.parentNode) old.parentNode.replaceChild(card, old); else deck.insertBefore(card, deck.firstChild);
      panels[id] = { node: card, action: action };
      var cards = deck.querySelectorAll('.ktv-card'); // até 3 guardados; o mais novo em destaque
      for (var i = 0; i < cards.length; i++) { cards[i].classList.toggle('is-lead', i === 0); if (i > 2) close(findId(cards[i])); }
      root.classList.toggle('has-panel', true);
      focus();
      animateCard(card);
      if (!old && !silent) enter(card);
    }
    // update_panel junta props; present_patch troca só as seções que vieram (pelo id), o resto fica
    function mergeAction(base, patch) {
      var merged = {}, k; for (k in base) merged[k] = base[k];
      if (patch.props) { merged.props = {}; for (k in (base.props || {})) merged.props[k] = base.props[k]; for (k in patch.props) merged.props[k] = patch.props[k]; }
      if (patch.sections) {
        var byId = {}; arr(patch.sections, 12).forEach(function (s) { byId[s.id] = s; });
        merged.sections = arr(base.sections, 12).map(function (s) { var n = byId[s.id]; if (!n) return s; var m = {}; for (k in s) m[k] = s[k]; for (k in n) m[k] = n[k]; delete byId[s.id]; return m; });
        for (k in byId) merged.sections.push(byId[k]);
      }
      return merged;
    }
    function findId(node) { for (var k in panels) if (panels[k].node === node) return k; return ''; }
    function close(id) {
      var p = panels[id]; if (!p) return; delete panels[id];
      var n = p.node; n.classList.add('is-leaving'); stopCard(n);
      var rest = root.querySelectorAll('.ktv-deck .ktv-card:not(.is-leaving)');
      if (rest.length) rest[0].classList.add('is-lead'); // o anterior volta ao destaque
      setTimeout(function () { if (n.parentNode) n.parentNode.removeChild(n); root.classList.toggle('has-panel', Object.keys(panels).length > 0); focus(); }, 420);
    }
    // Música tocando NA TV (2026-11-07): m.tv + m.videoId = player do YouTube embutido (som e vídeo na TV). O mesmo
    // vídeo não recria o player (só troca o rótulo); pausar/continuar vão por postMessage (enablejsapi).
    var curVid = '';
    function ytCmd(func) {
      var f = root.querySelector('.ktv-video');
      if (f && f.contentWindow) f.contentWindow.postMessage(JSON.stringify({ event: 'command', func: func, args: [] }), '*');
    }
    function setMusic(m) {
      var box = root.querySelector('.ktv-music');
      var vid = m && m.tv && /^[\w-]{6,20}$/.test(str(m.videoId)) ? str(m.videoId) : '';
      var keep = vid && vid === curVid ? box.querySelector('.ktv-video') : null;
      box.innerHTML = ''; curVid = vid;
      box.classList.toggle('is-video', !!vid);
      root.classList.toggle('has-music', !!(m && m.title));
      focus();
      if (!m || !m.title) return;
      var cover = img(m.cover);
      if (keep) box.appendChild(keep);
      else if (vid) {
        var fr = el('iframe', 'ktv-video'); fr.allow = 'autoplay; encrypted-media'; fr.setAttribute('frameborder', '0');
        fr.src = 'https://www.youtube.com/embed/' + vid + '?autoplay=1&playsinline=1&rel=0&modestbranding=1&iv_load_policy=3&enablejsapi=1';
        box.appendChild(fr);
      } else if (cover) { var im = el('img', 'ktv-cover'); im.alt = ''; im.onerror = function () { var e = el('div', 'ktv-cover is-empty', '♪'); if (im.parentNode) im.parentNode.replaceChild(e, im); }; im.src = cover; box.appendChild(im); } else box.appendChild(el('div', 'ktv-cover is-empty', '♪'));
      var info = el('div', 'ktv-track'); info.appendChild(el('strong', '', str(m.title, 120))); info.appendChild(el('span', '', str(m.artist, 120)));
      var eq = el('div', 'ktv-eq' + (m.playing === false ? ' is-paused' : '')); for (var i = 0; i < 5; i++) eq.appendChild(el('i'));
      var st = el('div', 'ktv-mstate'); st.appendChild(icon('music')); st.appendChild(el('span', '', m.playing === false ? 'Pausado' : 'Tocando agora')); st.appendChild(eq);
      info.insertBefore(st, info.firstChild); box.appendChild(info); if (!keep) enter(box);
    }
    // Aviso/lembrete que chegou com a Kira na TV: cartão no alto por 9 s
    function toast(m) {
      var host = document.querySelector('.ktv-toasts');
      if (!host) { host = el('div', 'ktv-toasts'); document.body.appendChild(host); }
      var t = el('div', 'ktv-toast'), tx = el('div');
      t.appendChild(icon('bell')); tx.appendChild(el('strong', '', str(m.title || 'Aviso', 60))); tx.appendChild(el('span', '', str(m.text, 300)));
      t.appendChild(tx); host.appendChild(t);
      requestAnimationFrame(function () { requestAnimationFrame(function () { t.classList.add('is-on'); }); });
      setTimeout(function () { t.classList.remove('is-on'); setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 500); }, 9000);
    }

    // Kira trancada no PC: a TV cobre tudo com relógio + "Kira bloqueada" (a voz continua respondendo)
    var lockBox = null;
    function tvLock(on) {
      if (!on) { if (lockBox) { lockBox.remove(); lockBox = null; } return; }
      if (lockBox) return;
      lockBox = el('div', 'ktv-lock');
      lockBox.style.cssText = 'position:fixed;inset:0;z-index:99999;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#05030a;color:#e9d5ff;font-weight:200';
      var clock = el('div'); clock.style.cssText = 'font-size:8vw;text-shadow:0 0 24px #d946ef';
      var label = el('div', '', 'Kira bloqueada'); label.style.cssText = 'font-size:2.4vw;opacity:.6';
      lockBox.appendChild(clock); lockBox.appendChild(label); document.body.appendChild(lockBox);
      (function tick() { if (!lockBox) return; var d = new Date(); clock.textContent = ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2); setTimeout(tick, 5000); })();
    }

    var api = {
      handle: function (msg) {
        if (!msg || typeof msg !== 'object') return;
        if (msg.t === 'lock' || (msg.t === 'hello' && 'locked' in msg)) tvLock(msg.t === 'lock' ? msg.on : msg.locked);
        // o orbe da TV só existe onde a voz sai: voz no PC = orbe some daqui (encolhe pro centro); voz na TV = surge
        if (msg.t === 'audio' || msg.t === 'hello') { voiceHere = !!(msg.t === 'audio' ? msg.on : msg.audio); root.classList.toggle('voice-here', voiceHere); }
        if (msg.t === 'state') { state = STATE_LABEL[msg.state] ? msg.state : 'idle'; target = state === 'speaking' ? Math.max(0, Math.min(1, Number(msg.level) || 0.4)) : 0; root.className = root.className.replace(/is-\w+/, 'is-' + state); tick(); if (!raf) raf = requestAnimationFrame(frame); }
        else if (msg.t === 'modelo' || msg.t === 'hello') { if (MODELS[msg.n || msg.modelo]) { model = Number(msg.n || msg.modelo); build(); } if (msg.t === 'hello' && msg.music) setMusic(msg.music); }
        else if (msg.t === 'music') setMusic(msg);
        else if (msg.t === 'music_cmd') ytCmd(msg.cmd === 'pause' ? 'pauseVideo' : 'playVideo');
        else if (msg.t === 'aviso') toast(msg);
        else if (msg.t === 'scroll' && window.KiraTvPanels) window.KiraTvPanels.scroll(msg.dir);
        else if (msg.t === 'call' && window.KiraTvPanels) window.KiraTvPanels.call(msg);
        else if (msg.t === 'ui' && msg.action && window.KiraTvPanels) window.KiraTvPanels.dispatch(msg.action); // mesmos painéis do PC
        else if (msg.t === 'ui' && msg.action) {
          var a = msg.action;
          if (a.type === 'close_panel') close(str(a.id || a.presentationId, 64));
          else if (a.type === 'open_panel' || a.type === 'present') place(a);
          else if ((a.type === 'update_panel' || a.type === 'present_patch')) { var id = str(a.id || a.presentationId, 64); if (panels[id]) { place(mergeAction(panels[id].action, a), true); } }
        }
      },
      setExternal: function (n) { external = Number(n) || 0; root.classList.toggle('has-panel', external > 0 || Object.keys(panels).length > 0); focus(); },
      setModel: function (n) { if (MODELS[n]) { model = Number(n); build(); } },
      setVisible: function (v) { visible = !!v; if (visible && !raf) raf = requestAnimationFrame(frame); },
      get model() { return model; }
    };
    build();
    setInterval(tick, 10000);
    raf = requestAnimationFrame(frame);
    if (opts.demo) demo(api);
    return api;
  }

  /* ---------- modo demonstração: ciclo com tudo ---------- */
  var DEMO = [
    { t: 'state', state: 'listening', level: 0.5 }, { t: 'state', state: 'thinking' },
    { t: 'state', state: 'speaking', level: 0.8 },
    { t: 'ui', action: { type: 'open_panel', kind: 'quadro', id: 'd1', title: 'Clima em Fortaleza', props: { tipo: 'info', resumo: 'Sol com poucas nuvens o dia todo, brisa do mar à tarde.', itens: [{ rotulo: 'Agora', valor: '29°' }, { rotulo: 'Máxima', valor: '31°' }, { rotulo: 'Chuva', valor: '10%' }], destaque: 'Leve protetor: índice UV alto.' } } },
    { t: 'state', state: 'idle' },
    { t: 'music', title: 'Lua de Cristal', artist: 'Xuxa', cover: '', playing: true },
    { t: 'ui', action: { type: 'open_panel', kind: 'quadro', id: 'd2', title: 'Português · PM', props: { tipo: 'questoes', questoes: [{ enunciado: 'Assinale a frase com crase correta.', alternativas: ['Vou à praia.', 'Vou à pé.', 'Refiro-me à você.'], resposta: 0, explicacao: 'Antes de palavra feminina regida por "a".' }] } } },
    { t: 'state', state: 'speaking', level: 0.6 },
    { t: 'ui', action: { type: 'open_panel', kind: 'quadro', id: 'd3', title: 'Gasto da semana', props: { tipo: 'grafico', dados: [{ rotulo: 'Mercado', valor: 320 }, { rotulo: 'Gasolina', valor: 180 }, { rotulo: 'Lazer', valor: 90 }] } } },
    { t: 'ui', action: { type: 'close_panel', id: 'd1' } },
    { t: 'ui', action: { type: 'open_panel', kind: 'quadro', id: 'd4', title: 'Concurso do IBGE abre 27 mil vagas', props: { tipo: 'noticia', subtitulo: 'Inscrições começam na próxima semana', artigo: ['Os salários chegam a R$ 3.100 e há vagas em todos os estados.'], fonte: 'g1' } } },
    { t: 'state', state: 'idle' },
    { t: 'ui', action: { type: 'close_panel', id: 'd2' } }, { t: 'ui', action: { type: 'close_panel', id: 'd3' } },
    { t: 'ui', action: { type: 'close_panel', id: 'd4' } }, { t: 'music' }
  ];
  function demo(api) { var i = 0; setInterval(function () { api.handle(DEMO[i % DEMO.length]); i++; }, 2600); }

  /* ---------- na TV: eventos da Kira + batimento ---------- */
  function connect(api) {
    var id = Math.random().toString(36).slice(2, 10), es = null;
    function open() {
      try { es = new EventSource('/events'); } catch (e) { setTimeout(open, 3000); return; }
      es.onmessage = function (ev) { try { api.handle(JSON.parse(ev.data)); } catch (e) { /* evento estragado: ignora */ } };
      es.onerror = function () { es.close(); setTimeout(open, 3000); };
    }
    open();
    setInterval(function () { try { fetch('/alive?id=' + id + '&modelo=' + api.model).catch(function () {}); } catch (e) { /* sem fetch */ } }, 3000);
  }

  window.KiraTV = { mount: mount, MODELS: MODELS, connect: connect, DEMO: DEMO };
})();
