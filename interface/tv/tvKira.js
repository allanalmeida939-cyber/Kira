// TV = a MESMA Kira do PC (2026-11-06): painéis, apresentações (clima, briefing, notícias), Olho de Deus e rota
// usam os módulos de UI do PC (o navegador da TV é Chrome 120). A TV só troca a moldura (orbe Cinema, sem
// transcrição, sem sidebar). Ações chegam pelo /events (tv.js repassa) e as chamadas de ida e volta respondem em /rpc.
import { createEventBus } from '/assets/js/ui/core/eventBus.js';
import { createDispatcher } from '/assets/js/ui/core/dispatcher.js';
import { createPanelRegistry } from '/assets/js/ui/windowing/panelRegistry.js';
import { WindowManager } from '/assets/js/ui/windowing/WindowManager.js';
import '/assets/js/ui/godseye/godsEye.js';

// Na TV os painéis aparecem prontos, sem a animação de sair do orbe (2026-11-07: entravam clarinhos, piscando e se
// mexendo antes de firmar; o navegador da TV não dá conta da animação).
document.documentElement.dataset.kiraMotion = 'off';
const layer = document.createElement('div');
layer.className = 'kui-window-layer tv-kira-layer';
document.body.appendChild(layer);
const bus = createEventBus();
const windowManager = new WindowManager(layer, { registry: createPanelRegistry(), bus });
const dispatcher = createDispatcher({ windowManager, bus });

// quantos conteúdos estão na tela (painéis abertos + Olho de Deus): o tv.js põe o orbe no canto e esconde o relógio
const count = () => layer.querySelectorAll('.kui-window:not(.is-closing)').length
  + (document.documentElement.classList.contains('gods-eye-active') ? 1 : 0);
// Layout de TV (2026-11-07, revisão de design): o painel mais recente fica em destaque no CENTRO, grande (o conteúdo
// ganha zoom para ler a 3 m); os outros viram miniaturas numa fileira embaixo. O canto de cima à direita fica livre
// para o orbe. Só classes: o WindowManager continua dono das animações (transform) e o CSS vence o left/top dele.
const layout = () => {
  const wins = [...layer.querySelectorAll('.kui-window:not(.is-closing)')]
    .sort((a, b) => (Number(b.style.zIndex) || 0) - (Number(a.style.zIndex) || 0));
  wins.forEach((w, i) => {
    w.classList.toggle('tv-featured', i === 0);
    w.classList.toggle('tv-mini', i > 0 && i <= 5);
    w.classList.toggle('tv-hidden', i > 5);
    if (i > 0) w.style.setProperty('--tv-mini-i', String(i - 1));
    // o zoom do fitTv fica no estilo da caixa: ao virar miniatura, tira (senão a miniatura mostrava o texto gigante cortado)
    if (i > 0) { const c = w.querySelector('.kui-content'); if (c && c.style.zoom) c.style.zoom = ''; }
  });
  document.body.classList.toggle('tv-has-minis', wins.length > 1);
};
let last = -1;
const notify = () => { layout(); const n = count(); if (n !== last) { last = n; window.KiraTVApi?.setExternal?.(n); } };
new MutationObserver(notify).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'hidden'] });

window.KiraTvPanels = {
  dispatch(action) { const result = dispatcher.dispatch(action); setTimeout(notify, 60); return result; },
  // {"t":"call", id, target, method, args}: mesmos alvos do celular (Olho de Deus, rota)
  call(msg) {
    const targets = { godseye: window.KiraGodsEye, rota: window.KiraRouteOverlay };
    let result = null;
    try { result = targets[msg.target]?.[msg.method]?.(msg.args) ?? null; } catch (_) { result = null; }
    Promise.resolve(result).then((value) => {
      const full = value && typeof value === 'object' ? value : { ok: Boolean(value) };
      // só o que o backend lê (o resultado inteiro pode ser grande demais para ir no endereço)
      const body = {};
      ['ok', 'mode', 'map', 'layers', 'focus', 'error'].forEach((key) => { if (full[key] !== undefined) body[key] = full[key]; });
      fetch(`/rpc?id=${encodeURIComponent(msg.id)}&r=${encodeURIComponent(JSON.stringify(body))}`).catch(() => {});
      setTimeout(notify, 300);
    });
  },
};

// TV sem rolagem (2026-11-07, refeito): o painel em destaque encolhe por igual (zoom) até TUDO caber, inclusive o que
// rola por dentro (a lista de questões do simulado é uma caixa com rolagem própria: antes o fitTv só olhava a caixa de
// fora e a questão 5 ficava cortada). Mede só quando o conteúdo muda (antes zerava o zoom a cada 0,7 s e refazia a
// tela inteira, o que engasgava a TV). Zoom base 2,1 (tv.css); mínimo 0,85. Se nem assim couber, "rola pra baixo".
const BASE_ZOOM = 2.1;
const MIN_ZOOM = 0.85;
const overflowers = (box) => [box, ...box.querySelectorAll('*')].filter((n) => n.clientHeight > 30
  && n.scrollHeight > n.clientHeight + 3 && (n === box || /auto|scroll|hidden/.test(getComputedStyle(n).overflowY)));
const need = (box) => Math.max(1, box.scrollWidth / Math.max(1, box.clientWidth),
  ...overflowers(box).map((n) => n.scrollHeight / Math.max(1, n.clientHeight)));
let fitTimer = 0;
function fitTv() {
  fitTimer = 0;
  const box = document.querySelector('.tv-kira-layer .kui-window.tv-featured .kui-content');
  if (!box) return;
  let zoom = BASE_ZOOM;
  box.style.zoom = String(zoom);
  // a largura também muda com o zoom (o texto quebra menos): mede de novo até 4 vezes
  for (let i = 0; i < 4; i += 1) {
    const k = need(box);
    if (k <= 1.01 || zoom <= MIN_ZOOM) break;
    zoom = Math.max(MIN_ZOOM, zoom / k);
    box.style.zoom = zoom.toFixed(3);
  }
  const over = need(box) > 1.01;
  if (over !== box.classList.contains('tv-overflow')) box.classList.toggle('tv-overflow', over);
}
const scheduleFit = () => { if (!fitTimer) fitTimer = setTimeout(fitTv, 80); };
new MutationObserver(scheduleFit).observe(layer, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['class'] });
layer.addEventListener('load', scheduleFit, true); // imagem que chega depois muda a altura
setInterval(scheduleFit, 5000); // rede de segurança (barato: só mede)

// "rola pra baixo / desce mais / sobe" (tv_tela acao='rolar'): rola o maior trecho que ainda não coube
window.KiraTvPanels.scroll = (dir) => {
  const box = document.querySelector('.tv-kira-layer .kui-window.tv-featured .kui-content');
  if (!box) return false;
  const target = overflowers(box).sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0];
  if (!target) return false;
  const step = Math.round(target.clientHeight * 0.8) * (/cima|sobe|up/.test(String(dir)) ? -1 : 1);
  target.scrollBy({ top: /topo|inicio|início/.test(String(dir)) ? -target.scrollHeight : step, behavior: 'smooth' });
  return true;
};

// Abas pedidas com a Kira na TV (o backend só manda casa e música; WhatsApp e configurações nunca vêm para cá)
globalThis.KiraUiControl = {
  openTab(tab) {
    if (tab === 'casa') { dispatcher.dispatch({ type: 'open_panel', kind: 'smart_home', title: 'Casa inteligente', view: 'main' }); setTimeout(notify, 60); return { ok: true }; }
    return { ok: tab === 'music' }; // a música já aparece no palco da TV (tv.js)
  },
  musicCommand() { return { ok: false }; },
};
