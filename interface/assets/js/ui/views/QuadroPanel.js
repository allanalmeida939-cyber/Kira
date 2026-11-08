// Quadro: painel que a Kira cria na hora sobre qualquer coisa (2026-11-05). Tipos: info, lista, grafico (SVG próprio),
// questoes (clicáveis), noticia, tabela, flashcards. 2026-11-07 (Fase 1F): 6 variações visuais (props.estilo), sorteadas
// no servidor: solto (sem caixa nem barra, "PNG"), janela, cards, faixa, destaque (número/item grande) e editorial (papel
// claro). O conteúdo nunca rola: a fonte se ajusta ao tamanho do painel (PC e TV). Tudo entra como texto (textContent).
import { clear, create, safeImage, safeText } from './panelDom.js';

const SVG = 'http://www.w3.org/2000/svg';
const arr = (v, n = 40) => (Array.isArray(v) ? v.slice(0, n) : []);
export const QUADRO_VARIACOES = Object.freeze(['solto', 'janela', 'cards', 'faixa', 'destaque', 'editorial']);
const ANTIGOS = { a: 'janela', b: 'cards', c: 'faixa' }; // estilos fixados antes de 2026-11-07
// variações sem barra de título (o título vai grande dentro do conteúdo; a barra aparece só com o mouse por cima)
const SEM_BARRA = new Set(['solto', 'destaque', 'editorial']);
// teto da fonte: as soltas/destaque podem crescer mais (tipografia forte); as demais ficam compactas
const FONTE_MAX = { solto: 28, destaque: 26, editorial: 24 };

export function quadroVariacao(estilo) {
  const v = safeText(estilo).toLowerCase();
  return QUADRO_VARIACOES.includes(v) ? v : ANTIGOS[v] || 'janela';
}

function chart(doc, props) {
  const data = arr(props.dados, 24).map((d) => ({ r: safeText(d?.rotulo ?? d?.label).slice(0, 24), v: Number(d?.valor ?? d?.value) || 0 }));
  const max = Math.max(1, ...data.map((d) => Math.abs(d.v)));
  const svg = doc.createElementNS(SVG, 'svg');
  // rótulos numa coluna do tamanho do maior, alinhados à direita, antes da barra
  const W = 520; const rowH = 34; const H = props.formato === 'linha' ? 240 : data.length * rowH + 8;
  const labelW = Math.min(200, Math.max(40, ...data.map((d) => d.r.length * 10)));
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('class', 'kui-quadro-chart');
  const add = (tag, attrs, text) => { const n = doc.createElementNS(SVG, tag); Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v)); if (text) n.textContent = text; svg.appendChild(n); return n; };
  if (props.formato === 'linha' && data.length > 1) {
    const x = (i) => 30 + (i * (W - 60)) / (data.length - 1); const y = (v) => 190 - (v / max) * 170;
    add('polyline', { points: data.map((d, i) => `${x(i)},${y(d.v)}`).join(' '), class: 'kui-quadro-line' });
    data.forEach((d, i) => { add('circle', { cx: x(i), cy: y(d.v), r: 5, class: 'kui-quadro-dot' }); add('text', { x: x(i), y: 214, 'text-anchor': 'middle' }, d.r); });
  } else {
    data.forEach((d, i) => {
      const w = Math.max(2, (Math.abs(d.v) / max) * (W - labelW - 80));
      add('text', { x: labelW, y: i * rowH + 23, 'text-anchor': 'end' }, d.r);
      add('rect', { x: labelW + 10, y: i * rowH + 6, width: w, height: rowH - 12, rx: 6, class: `kui-quadro-bar${d.v === max ? ' is-top' : ''}` });
      add('text', { x: labelW + 18 + w, y: i * rowH + 23, class: 'kui-quadro-val' }, String(d.v));
    });
  }
  return { svg, top: data.reduce((a, d) => (!a || d.v > a.v ? d : a), null) };
}

function question(doc, q, index, refit) {
  const box = create(doc, 'div', 'kui-quadro-q');
  box.appendChild(create(doc, 'p', 'kui-quadro-stem', `${index + 1}. ${safeText(q?.enunciado)}`));
  const answer = Number(q?.resposta);
  const why = create(doc, 'p', 'kui-quadro-why');
  const alts = create(doc, 'div', 'kui-quadro-alts');
  arr(q?.alternativas, 6).forEach((alt, i) => {
    const b = create(doc, 'button', 'kui-quadro-alt');
    b.type = 'button';
    b.append(create(doc, 'b', 'kui-quadro-letra', 'ABCDEF'[i]), create(doc, 'span', '', safeText(alt)));
    b.addEventListener('click', () => {
      box.querySelectorAll('.kui-quadro-alt').forEach((x, j) => { x.disabled = true; if (j === answer) x.classList.add('is-right'); });
      if (i !== answer) b.classList.add('is-wrong');
      why.textContent = `${i === answer ? 'Certa!' : `Era a ${'ABCDEF'[answer] || '?'}.`} ${safeText(q?.explicacao)}`;
      refit();
    });
    alts.appendChild(b);
  });
  box.append(alts, why);
  return box;
}

// Flashcard: clica e vira; com id (vindo da professora), Acertei/Errei marcam a próxima revisão (caixas de Leitner).
function flashcard(doc, card, refit) {
  const box = create(doc, 'div', 'kui-quadro-card');
  const face = create(doc, 'button', 'kui-quadro-face', safeText(card?.frente));
  face.type = 'button';
  const back = create(doc, 'p', 'kui-quadro-back', safeText(card?.verso));
  const row = create(doc, 'div', 'kui-quadro-grade');
  back.hidden = true; row.hidden = true;
  face.addEventListener('click', () => { back.hidden = !back.hidden; row.hidden = back.hidden || !card?.id; box.classList.toggle('is-open', !back.hidden); refit(); });
  [['Acertei', true], ['Errei', false]].forEach(([label, ok]) => {
    const b = create(doc, 'button', `kui-quadro-alt ${ok ? 'is-right' : 'is-wrong'}`, label);
    b.type = 'button';
    b.addEventListener('click', () => {
      row.querySelectorAll('button').forEach((x) => { x.disabled = true; });
      fetch('/api/tutor/card', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: card.id, acertou: ok }) })
        .then((r) => r.json()).then((r) => { back.textContent += r?.ok ? ` — volta em ${r.dias || 0} dia(s)` : ''; }).catch(() => {});
    });
    row.append(b);
  });
  box.append(face, back, row);
  return box;
}

/** Maior fonte (px) com que o conteúdo cabe sem rolar; busca binária (7 medidas). */
export function fitFont(box, max = 19, min = 8) {
  const H = box.clientHeight; const W = box.clientWidth;
  if (!H || !W) return null;
  let lo = min; let hi = max;
  for (let i = 0; i < 7; i += 1) {
    const mid = (lo + hi) / 2;
    box.style.fontSize = `${mid}px`;
    if (box.scrollHeight <= box.clientHeight && box.scrollWidth <= box.clientWidth) lo = mid; else hi = mid;
  }
  box.style.fontSize = `${Math.floor(lo * 10) / 10}px`;
  return lo;
}

export function createQuadroPanel() {
  return {
    mount(container, context) {
      const doc = container.ownerDocument;
      const view = doc?.defaultView;
      const host = container.closest?.('.kui-window');
      const el = (tag, cls, text) => create(doc, tag, cls, text);
      let variacao = 'janela';
      let frame = 0;
      const refit = () => {
        if (!view?.requestAnimationFrame) return;
        view.cancelAnimationFrame(frame);
        frame = view.requestAnimationFrame(() => fitFont(container, FONTE_MAX[variacao] || 23));
      };
      const render = (p = {}) => {
        clear(container);
        variacao = quadroVariacao(p.estilo);
        const tipo = safeText(p.tipo, 'info');
        container.className = `kui-content kui-quadro is-${tipo} is-v-${variacao}`;
        if (host) { host.classList.add('kui-quadro-host'); host.dataset.quadroV = variacao; host.classList.toggle('is-quadro-sem-barra', SEM_BARRA.has(variacao)); }
        const titulo = safeText(p.titulo || context?.get?.()?.title);
        // título dentro do conteúdo: some nas variações com barra (lá ele já está na barra), menos na notícia (manchete)
        if (titulo && (SEM_BARRA.has(variacao) || tipo === 'noticia')) container.append(el('h2', 'kui-quadro-titulo', titulo.replace(/ · \d+$/, '')));
        const img = safeImage(p.imagem);
        if (tipo === 'noticia' && img) { const i = el('img', 'kui-quadro-capa'); i.src = img; i.alt = ''; i.addEventListener('load', refit); container.append(i); }
        if (p.subtitulo) container.append(el('p', 'kui-quadro-sub', safeText(p.subtitulo)));
        if (p.resumo) container.append(el('p', 'kui-quadro-resumo', safeText(p.resumo)));
        if (tipo === 'lista') {
          const list = el(p.numerada ? 'ol' : 'ul', 'kui-quadro-list');
          arr(p.itens).forEach((it, i) => { const li = el('li'); li.append(el('b', 'kui-quadro-n', p.numerada ? String(i + 1).padStart(2, '0') : '•'), el('span', '', safeText(it))); list.append(li); });
          container.append(list);
        } else if (tipo === 'grafico') {
          const { svg, top } = chart(doc, p);
          if (top) { const kpi = el('p', 'kui-quadro-kpi'); kpi.append(el('b', '', String(top.v)), el('span', '', top.r)); container.append(kpi); }
          container.append(svg);
        } else if (tipo === 'flashcards') {
          const deck = el('div', 'kui-quadro-deck'); arr(p.cards, 20).forEach((c) => deck.append(flashcard(doc, c, refit))); container.append(deck);
        } else if (tipo === 'questoes') arr(p.questoes, 10).forEach((q, i) => container.append(question(doc, q, Number(p.inicio) + i || i, refit)));
        else if (tipo === 'tabela') {
          const t = el('table', 'kui-quadro-table'); const head = el('tr');
          arr(p.colunas, 8).forEach((c) => head.append(el('th', '', safeText(c)))); t.append(head);
          arr(p.linhas, 30).forEach((row) => { const tr = el('tr'); arr(row, 8).forEach((c) => tr.append(el('td', '', safeText(c)))); t.append(tr); });
          container.append(t);
        } else if (tipo === 'noticia') {
          const body = el('div', 'kui-quadro-artigo'); arr(p.artigo, 12).forEach((par) => body.append(el('p', 'kui-quadro-par', safeText(par)))); container.append(body);
        }
        if (arr(p.itens).length && tipo === 'info') {
          const facts = el('div', 'kui-quadro-facts');
          arr(p.itens, 12).forEach((it) => { const f = el('div', 'kui-quadro-fact'); f.append(el('span', 'kui-quadro-k', safeText(it?.rotulo)), el('b', 'kui-quadro-v', safeText(it?.valor))); facts.append(f); });
          container.append(facts);
        }
        if (p.destaque) container.append(el('p', 'kui-quadro-destaque', safeText(p.destaque)));
        if (p.fonte) container.append(el('p', 'kui-quadro-fonte', `fonte: ${safeText(p.fonte)}`));
        refit();
      };
      render(context.props);
      // tamanho de FORA (border-box): o de dentro muda com a própria fonte (padding em em) e virava um laço de ajustes
      let seen = '';
      const watcher = view?.ResizeObserver ? new view.ResizeObserver(([entry]) => {
        const box = entry?.borderBoxSize?.[0]; const key = box ? `${Math.round(box.inlineSize)}x${Math.round(box.blockSize)}` : '';
        if (key !== seen) { seen = key; refit(); }
      }) : null;
      watcher?.observe(container, { box: 'border-box' });
      container.__kuiQuadro = { update: render };
      return () => {
        watcher?.disconnect();
        view?.cancelAnimationFrame?.(frame);
        host?.classList.remove('kui-quadro-host', 'is-quadro-sem-barra');
        delete container.__kuiQuadro;
      };
    },
    update(container, props) { container.__kuiQuadro?.update?.(props); },
  };
}
