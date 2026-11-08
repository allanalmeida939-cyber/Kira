// Entradas da rota (routeView.js): 10 jeitos de a rota surgir na tela + a junção LiDAR + Neural + Energia Viva.
// Cada entrada recebe ctx = { root, mapWrap, drawRoute(how, ms), routePoints(n), later(fn, ms) } e cuida só da
// animação; os efeitos ficam em elementos .krv-fx por cima da rota e somem no fim. Visual em route-intros.css.
(function () {
  const rand = (a, b) => a + Math.random() * (b - a);

  function start(ctx, name) {
    const { root } = ctx;
    root.querySelectorAll('.krv-fx').forEach((node) => node.remove());
    root.dataset.intro = name;
    root.classList.remove('intro-run', 'intro-done');
    void root.offsetWidth;
    root.classList.add('intro-run');
  }
  function finish(ctx, ms) {
    ctx.later(() => {
      ctx.root.classList.add('intro-done');
      ctx.root.querySelectorAll('.krv-fx').forEach((node) => node.remove());
    }, ms);
  }
  function fx(root, cls) {
    const node = document.createElement('div');
    node.className = `krv-fx ${cls}`;
    node.setAttribute('aria-hidden', 'true');
    root.appendChild(node);
    return node;
  }
  // desenho em meia resolução (o navegador amplia): neste PC sem placa de vídeo cada pixel custa; a olho é igual
  const CANVAS_SCALE = 0.5;
  /** Tela de desenho do tamanho da rota, com laço de quadros até o fim (ms). draw recebe coordenadas em pixels cheios. */
  function canvas(ctx, ms, draw) {
    const c = document.createElement('canvas');
    c.className = 'krv-fx krv-fx-canvas';
    const box = ctx.root.getBoundingClientRect();
    c.width = Math.max(1, Math.round(box.width * CANVAS_SCALE));
    c.height = Math.max(1, Math.round(box.height * CANVAS_SCALE));
    ctx.root.appendChild(c);
    const g = c.getContext('2d');
    const t0 = performance.now();
    const loop = (now) => {
      if (!c.isConnected) return;
      const t = Math.min(1, (now - t0) / ms);
      g.setTransform(1, 0, 0, 1, 0, 0);
      g.clearRect(0, 0, c.width, c.height);
      g.setTransform(CANVAS_SCALE, 0, 0, CANVAS_SCALE, 0, 0);
      g.globalCompositeOperation = 'lighter'; // brilho somado sem mix-blend-mode (que repinta a tela inteira)
      draw(g, t, box.width, box.height);
      if (t < 1) requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
    return c;
  }
  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - ((-2 * t + 2) ** 3) / 2);

  /** Rede neural: nós na rota e em volta, ligando-se aos vizinhos; reveal(x) = só até onde o laser passou. */
  function neuralNet(ctx, ms, { reveal = null, hue = 190 } = {}) {
    const box = ctx.root.getBoundingClientRect();
    const route = ctx.routePoints(26);
    const nodes = [...route.map((p) => ({ ...p, onRoute: true })),
      ...Array.from({ length: 40 }, () => ({ x: rand(0, box.width), y: rand(0, box.height), onRoute: false }))]
      .map((n, i) => ({ ...n, at: (n.x / box.width) * 0.45 + Math.random() * 0.25, i }));
    const links = [];
    nodes.forEach((a, i) => {
      nodes.map((b, j) => ({ j, d: Math.hypot(a.x - b.x, a.y - b.y) })).filter((o) => o.j !== i).sort((p, q) => p.d - q.d).slice(0, 3)
        .forEach((o) => { if (o.d < 190) links.push([i, o.j, Math.max(a.at, nodes[o.j].at) + 0.08]); });
    });
    return canvas(ctx, ms, (g, t, w) => {
      const fade = t > 0.75 ? 1 - (t - 0.75) / 0.25 : 1;
      const limit = reveal ? reveal(t) * w : Infinity;
      links.forEach(([i, j, at]) => {
        const a = nodes[i]; const b = nodes[j];
        if (t < at || a.x > limit || b.x > limit) return;
        const k = Math.min(1, (t - at) / 0.12);
        g.strokeStyle = `hsla(${hue}, 95%, 70%, ${0.35 * fade})`;
        g.lineWidth = 1;
        g.beginPath(); g.moveTo(a.x, a.y); g.lineTo(a.x + (b.x - a.x) * k, a.y + (b.y - a.y) * k); g.stroke();
      });
      nodes.forEach((n) => {
        if (t < n.at || n.x > limit) return;
        const pop = Math.min(1, (t - n.at) / 0.08);
        // brilho: um círculo largo e fraco por baixo (shadowBlur era o que mais derrubava os quadros)
        const r = (n.onRoute ? 3.2 : 2) * pop;
        g.fillStyle = n.onRoute ? `hsla(300, 95%, 70%, ${0.22 * fade})` : `hsla(${hue}, 95%, 65%, ${0.18 * fade})`;
        g.beginPath(); g.arc(n.x, n.y, r * 3, 0, Math.PI * 2); g.fill();
        g.fillStyle = n.onRoute ? `hsla(300, 95%, 82%, ${fade})` : `hsla(${hue}, 95%, 78%, ${0.85 * fade})`;
        g.beginPath(); g.arc(n.x, n.y, r, 0, Math.PI * 2); g.fill();
      });
    });
  }

  const intros = {
    // 01 · partículas se juntam e formam o mapa; a rota vem como feixe de energia
    quantica(ctx) {
      start(ctx, 'quantica');
      const box = ctx.root.getBoundingClientRect();
      const targets = ctx.routePoints(140);
      const parts = Array.from({ length: 480 }, (_, i) => {
        const target = i < 420 && targets.length ? targets[i % targets.length] : { x: rand(0, box.width), y: rand(0, box.height) };
        const angle = rand(0, Math.PI * 2);
        const dist = rand(250, 700);
        return { sx: box.width / 2 + Math.cos(angle) * dist, sy: box.height / 2 + Math.sin(angle) * dist, tx: target.x + rand(-3, 3), ty: target.y + rand(-3, 3),
          delay: rand(0, 0.35), hue: i < 420 ? rand(280, 320) : rand(180, 210) };
      });
      canvas(ctx, 1900, (g, t) => {
        parts.forEach((p) => {
          const k = ease(Math.max(0, Math.min(1, (t - p.delay) / 0.6)));
          const fade = t > 0.7 ? 1 - (t - 0.7) / 0.3 : 1;
          g.fillStyle = `hsla(${p.hue}, 95%, 75%, ${0.85 * fade})`;
          g.fillRect(p.sx + (p.tx - p.sx) * k, p.sy + (p.ty - p.sy) * k, 2.6, 2.6);
        });
      });
      ctx.later(() => ctx.drawRoute('energy', 1500), 1000);
      finish(ctx, 2700);
    },
    // 02 · um laser varre da esquerda para a direita revelando tudo
    lidar(ctx) {
      start(ctx, 'lidar');
      fx(ctx.root, 'krv-fx-laser');
      ctx.later(() => ctx.drawRoute('line'), 900);
      finish(ctx, 2000);
    },
    // 03 · fenda roxa vertical que se abre para os lados
    portal(ctx) {
      start(ctx, 'portal');
      fx(ctx.root, 'krv-fx-slit');
      ctx.later(() => ctx.drawRoute('line'), 1200);
      finish(ctx, 2100);
    },
    // 04 · base de luz acende e o mapa sobe em perspectiva
    projecao(ctx) {
      start(ctx, 'projecao');
      fx(ctx.root, 'krv-fx-base');
      ctx.later(() => ctx.drawRoute('line'), 1300);
      finish(ctx, 2000);
    },
    // 05 · pontos viram neurônios, se ligam e formam as ruas; por último o caminho acende
    neural(ctx) {
      start(ctx, 'neural');
      neuralNet(ctx, 2000);
      ctx.later(() => ctx.drawRoute('line'), 1500);
      finish(ctx, 2100);
    },
    // 06 · mapa quase invisível; uma esfera elétrica sai do A e desenha o caminho até o B
    energia(ctx) {
      start(ctx, 'energia');
      ctx.later(() => ctx.drawRoute('energy', 2400), 350);
      finish(ctx, 3100);
    },
    // 07 · pulso circular do centro; o que a onda alcança vira mapa
    pulso(ctx) {
      start(ctx, 'pulso');
      fx(ctx.root, 'krv-fx-wave');
      fx(ctx.root, 'krv-fx-wave is-late');
      ctx.later(() => ctx.drawRoute('line'), 900);
      finish(ctx, 1900);
    },
    // 08 · placas de vidro flutuando em profundidades diferentes se encaixam
    vidro(ctx) {
      start(ctx, 'vidro');
      const wrap = fx(ctx.root, 'krv-fx-shards');
      const cols = 5;
      const rows = 3;
      for (let r = 0; r < rows; r += 1) {
        for (let c = 0; c < cols; c += 1) {
          const shard = document.createElement('i');
          shard.style.cssText = `left:${(c / cols) * 100}%;top:${(r / rows) * 100}%;width:${100 / cols}%;height:${100 / rows}%;`
            + `--z:${rand(-500, 300)}px;--rx:${rand(-50, 50)}deg;--ry:${rand(-60, 60)}deg;--tx:${rand(-120, 120)}px;--ty:${rand(-90, 90)}px;--d:${rand(0, 0.35)}s`;
          wrap.appendChild(shard);
        }
      }
      ctx.later(() => ctx.drawRoute('line'), 1300);
      finish(ctx, 2200);
    },
    // 09 · placa de luz dobrada que se desdobra para os lados e depois para cima e para baixo
    dobra(ctx) {
      start(ctx, 'dobra');
      fx(ctx.root, 'krv-fx-crease');
      ctx.later(() => ctx.drawRoute('line'), 1300);
      finish(ctx, 2000);
    },
    // 10 · anéis giram em volta de um núcleo, desaceleram, se alinham e projetam o mapa
    gravidade(ctx) {
      start(ctx, 'gravidade');
      const core = fx(ctx.root, 'krv-fx-core');
      ['a', 'b', 'c'].forEach((k) => core.appendChild(Object.assign(document.createElement('i'), { className: `ring-${k}` })));
      core.appendChild(Object.assign(document.createElement('b'), { className: 'nucleus' }));
      ctx.later(() => ctx.drawRoute('energy', 1500), 1500);
      finish(ctx, 3100);
    },
    // 02 + 05 + 06 · o laser varre e, por onde passa, a rede neural se forma; depois a esfera desenha a rota
    lidar_neural_energia(ctx) {
      start(ctx, 'lidar_neural_energia');
      fx(ctx.root, 'krv-fx-laser');
      neuralNet(ctx, 2300, { reveal: (t) => Math.min(1, t / 0.62) });
      ctx.later(() => ctx.drawRoute('energy', 2200), 1500);
      finish(ctx, 3800);
    },
  };


  // ---------- peças para juntar entradas (1 partículas, 2 laser, 3 portal, 4 projeção, 5 neural, 10 anéis) ----------
  /** Partículas que nascem num ponto, numa linha vertical ou fora da tela e pousam no caminho e no mapa. */
  function particles(ctx, ms, { from = 'outside', x = 0.5, y = 0.5, count = 300, hue = [280, 320] } = {}) {
    const box = ctx.root.getBoundingClientRect();
    const targets = ctx.routePoints(140);
    const parts = Array.from({ length: count }, (_, i) => {
      const target = i < count * 0.5 && targets.length ? targets[i % targets.length] : { x: rand(0, box.width), y: rand(0, box.height) };
      let sx; let sy;
      if (from === 'point') { sx = box.width * x + rand(-6, 6); sy = box.height * y + rand(-6, 6); }
      else if (from === 'line') { sx = box.width * x + rand(-3, 3); sy = rand(0, box.height); }
      else { const a = rand(0, Math.PI * 2); const d = rand(260, 700); sx = box.width / 2 + Math.cos(a) * d; sy = box.height / 2 + Math.sin(a) * d; }
      return { sx, sy, tx: target.x + rand(-3, 3), ty: target.y + rand(-3, 3), delay: rand(0, 0.3), hue: i < count * 0.5 ? rand(hue[0], hue[1]) : rand(180, 210),
        swirl: rand(-1, 1) };
    });
    return canvas(ctx, ms, (g, t) => {
      parts.forEach((p) => {
        const k = ease(Math.max(0, Math.min(1, (t - p.delay) / 0.62)));
        const fade = t > 0.72 ? 1 - (t - 0.72) / 0.28 : 1;
        const bend = Math.sin(k * Math.PI) * 60 * p.swirl;
        g.fillStyle = `hsla(${p.hue}, 95%, 76%, ${0.9 * fade})`;
        g.fillRect(p.sx + (p.tx - p.sx) * k + bend, p.sy + (p.ty - p.sy) * k - bend * 0.5, 2.6, 2.6);
      });
    });
  }
  /** Efeito em CSS que só nasce no seu momento (com atraso no CSS ele ficava parado na tela esperando a vez). */
  function timedFx(ctx, cls, at, dur, extra = '') {
    ctx.later(() => {
      const node = fx(ctx.root, `${cls} ${extra}`.trim());
      if (dur) node.style.animationDuration = `${dur}ms`;
    }, at);
  }
  /** Anéis gravitacionais num ponto (fração da caixa), girando, desacelerando e se alinhando. */
  function rings(ctx, at, { x = 0.5, y = 0.5, scale = 1 } = {}) {
    ctx.later(() => {
      const core = fx(ctx.root, 'krv-fx-core');
      core.style.left = `${x * 100}%`;
      core.style.top = `${y * 100}%`;
      core.style.transform = `scale(${scale})`;
      ['a', 'b', 'c'].forEach((k) => core.appendChild(Object.assign(document.createElement('i'), { className: `ring-${k}` })));
      core.appendChild(Object.assign(document.createElement('b'), { className: 'nucleus' }));
    }, at);
  }

  const combos = {
    // A · Gênese: anéis giram no centro → o núcleo explode em partículas que pousam no caminho → a fenda abre do
    // núcleo e o mapa sobe em 3D → o laser varre e acende a rede neural → a esfera desenha a rota
    combo_genese(ctx) {
      start(ctx, 'combo_genese');
      rings(ctx, 0);
      ctx.later(() => particles(ctx, 1600, { from: 'point', x: 0.5, y: 0.5 }), 1250);
      timedFx(ctx, 'krv-fx-slit', 1500, 1500);
      timedFx(ctx, 'krv-fx-base', 1600, 1600);
      timedFx(ctx, 'krv-fx-laser', 2700, 1300);
      ctx.later(() => neuralNet(ctx, 1800, { reveal: (t) => Math.min(1, t / 0.7) }), 2700);
      ctx.later(() => ctx.drawRoute('energy', 1800), 3500);
      finish(ctx, 5600);
    },
    // B · Projeção Tática: a base acende e o mapa sobe em 3D enquanto o laser varre; por onde ele passa a rede
    // neural se forma; a fenda risca o centro; partículas de fora formam o caminho; anéis se alinham sobre o B
    combo_tatica(ctx) {
      start(ctx, 'combo_tatica');
      timedFx(ctx, 'krv-fx-base', 0, 2000);
      timedFx(ctx, 'krv-fx-laser', 250, 1600);
      ctx.later(() => neuralNet(ctx, 2200, { reveal: (t) => Math.min(1, t / 0.6), hue: 170 }), 250);
      timedFx(ctx, 'krv-fx-slit', 1500, 1100, 'is-thin');
      ctx.later(() => particles(ctx, 1500, { from: 'outside', count: 260 }), 1700);
      const b = ctx.routePoints(2)[1];
      const box = ctx.root.getBoundingClientRect();
      if (b) rings(ctx, 2600, { x: b.x / box.width, y: b.y / box.height, scale: 0.45 });
      ctx.later(() => ctx.drawRoute('line', 1400), 2500);
      finish(ctx, 4900);
    },
    // C · Portal Neural: a fenda abre e despeja partículas → a rede neural cresce a partir da fenda → anéis se
    // alinham no centro e projetam o mapa em 3D → laser final de checagem → esfera desenha a rota
    combo_portal(ctx) {
      start(ctx, 'combo_portal');
      timedFx(ctx, 'krv-fx-slit', 0, 1800);
      ctx.later(() => particles(ctx, 1500, { from: 'line', x: 0.5, count: 300 }), 350);
      ctx.later(() => neuralNet(ctx, 1700, { hue: 280 }), 900);
      rings(ctx, 1500, { scale: 0.8 });
      timedFx(ctx, 'krv-fx-base', 2500, 1400);
      timedFx(ctx, 'krv-fx-laser', 3100, 1100);
      ctx.later(() => ctx.drawRoute('energy', 1700), 3300);
      finish(ctx, 5300);
    },
    // D · Núcleo LiDAR: núcleo no centro com anéis; dois lasers saem dele para os lados abrindo o mapa como um
    // portal; a base acende e o mapa inclina; partículas e rede neural se juntam no caminho; rota acende
    combo_nucleo(ctx) {
      start(ctx, 'combo_nucleo');
      rings(ctx, 0, { scale: 0.7 });
      timedFx(ctx, 'krv-fx-laser', 900, 1300, 'is-right');
      timedFx(ctx, 'krv-fx-laser', 900, 1300, 'is-left');
      timedFx(ctx, 'krv-fx-base', 1000, 1800);
      ctx.later(() => particles(ctx, 1600, { from: 'outside', count: 280, hue: [170, 200] }), 1500);
      ctx.later(() => neuralNet(ctx, 1700, { hue: 300 }), 1900);
      ctx.later(() => ctx.drawRoute('energy', 1500), 2900);
      finish(ctx, 4900);
    },
  };
  Object.assign(intros, combos);

  window.KiraRouteIntros = intros;
  window.KiraRouteIntroNames = {
    quantica: '01 · Materialização Quântica', lidar: '02 · Scanner LiDAR', portal: '03 · Portal Dimensional', projecao: '04 · Projeção 3D',
    neural: '05 · Construção Neural', energia: '06 · Rota de Energia Viva', pulso: '07 · Pulso Holográfico', vidro: '08 · Fragmentos de Vidro',
    dobra: '09 · Dobra Digital', gravidade: '10 · Núcleo Gravitacional', lidar_neural_energia: '02 + 05 + 06 · LiDAR + Neural + Energia',
    combo_genese: 'A · Gênese', combo_tatica: 'B · Projeção Tática', combo_portal: 'C · Portal Neural', combo_nucleo: 'D · Núcleo LiDAR',
  };
})();
