import { clear, create, safeImage, safeText } from './panelDom.js';

const LOAD_TIMEOUT_MS = 8000;
const SPOKEN_WINDOW_WORDS = 40;
// Falando há este tempo sem a imagem trocar (as palavras do momento não apareceram): avança uma. Antes a imagem empacava
// ou pulava direto para o fim da história (teste de Chernobyl: 1ª imagem aos 17 s, a 2ª já era a última).
export const BEAT_FALLBACK_MS = 10000;
const CAPTION_MAX = 90;

function setVisible(node, visible) {
  node.className = `kui-viewer-layer${visible ? ' is-visible' : ''}`;
}

const fold = (text) => String(text || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
const words = (text) => fold(text).split(/[^a-z0-9]+/).filter((word) => word.length > 3);
const shortCaption = (text) => {
  const value = safeText(text).replace(/\s+/g, ' ').trim();
  return value.length <= CAPTION_MAX ? value : `${value.slice(0, CAPTION_MAX - 1).replace(/\s+\S*$/, '')}…`;
};

/** Momento seguinte cujas palavras a Kira acabou de dizer (o texto vem sincronizado com o áudio). */
export function nextSpokenBeat(beats, current, spokenWords) {
  const heard = new Set(spokenWords);
  for (let index = current + 1; index < beats.length; index += 1) {
    const keys = beats[index].keywords || [];
    if (!keys.length) continue;
    const hits = keys.filter((key) => heard.has(key)).length;
    if (hits >= Math.min(2, keys.length)) return index;
  }
  return -1;
}

/** Carrega e decodifica a imagem antes da troca; uma segunda tentativa com parâmetro de cache. */
export async function loadImage(src, { imageFactory = () => new globalThis.Image(), timeoutMs = LOAD_TIMEOUT_MS, setTimer = globalThis.setTimeout, clearTimer = globalThis.clearTimeout } = {}) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const url = attempt === 0 ? src : `${src}${src.includes('&') ? '&' : '?'}retry=1`;
    const image = imageFactory();
    let timer = null;
    try {
      const ready = new Promise((resolve, reject) => {
        image.onerror = () => reject(new Error('imagem indisponível'));
        image.src = url;
        if (typeof image.decode === 'function') image.decode().then(resolve, reject);
        else image.onload = resolve;
      });
      const limit = new Promise((_, reject) => { timer = setTimer(() => reject(new Error('tempo esgotado')), timeoutMs); });
      await Promise.race([ready, limit]);
      return url;
    } catch (_) {
      /* tenta de novo uma vez */
    } finally {
      clearTimer(timer);
    }
  }
  return null;
}

// Variações do painel de imagens (2026-11-07): sorteadas a cada painel; "gostei, mantém" fixa (configs/quadro_estilos.json,
// tipo 'imagens', lido em /api/quadro/estilos). moldura = a de sempre; solto = só a imagem; cinema; polaroid; faixa.
export const VIEWER_VARIACOES = Object.freeze(['moldura', 'solto', 'cinema', 'polaroid', 'faixa']);

function applyViewerVariation(container, props, view) {
  const host = typeof container.closest === 'function' ? container.closest('.kui-window') : null;
  if (!host) return;
  const pick = (v) => { host.dataset.viewerV = v; };
  const asked = safeText(props?.estilo).toLowerCase();
  if (VIEWER_VARIACOES.includes(asked)) { pick(asked); return; }
  pick(VIEWER_VARIACOES[Math.floor(Math.random() * VIEWER_VARIACOES.length)]);
  if (typeof view?.fetch !== 'function') return;
  view.fetch('/api/quadro/estilos').then((r) => r.json()).catch(() => ({})).then((saved) => {
    const fixed = safeText(saved?.imagens).toLowerCase();
    if (VIEWER_VARIACOES.includes(fixed)) { pick(fixed); return; }
    // sem fixo: avisa qual saiu, para o "gostei, mantém esse" saber o que salvar
    view.fetch('/api/quadro/visto', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tipo: 'imagens', estilo: host.dataset.viewerV }) }).catch(() => {});
  });
}

export function createViewerPanel({ documentRef = globalThis.document, imageFactory, now = () => Date.now() } = {}) {
  return {
    mount(container, context) {
      const documentForPanel = container.ownerDocument || documentRef;
      const view = documentForPanel.defaultView || globalThis;
      clear(container);
      const stage = create(documentForPanel, 'div', 'kui-viewer-stage');
      const layers = [create(documentForPanel, 'img', 'kui-viewer-layer'), create(documentForPanel, 'img', 'kui-viewer-layer')];
      layers.forEach((layer) => { layer.alt = ''; stage.appendChild(layer); });
      const skeleton = create(documentForPanel, 'div', 'kui-viewer-skeleton');
      stage.appendChild(skeleton);
      // Sem legenda embaixo: a imagem é o foco. O crédito (licença) fica pequeno, por cima do canto da imagem.
      const caption = create(documentForPanel, 'p', 'kui-viewer-caption');
      caption.hidden = true;
      const credit = create(documentForPanel, 'p', 'kui-viewer-credit');
      const strip = create(documentForPanel, 'div', 'kui-viewer-thumbs');
      strip.hidden = true;
      const control = (cls, label, text, action) => {
        const button = create(documentForPanel, 'button', `kui-viewer-control ${cls}`, text);
        button.type = 'button';
        button.title = label;
        button.setAttribute?.('aria-label', label);
        button.addEventListener?.('click', action);
        stage.appendChild(button);
        return button;
      };
      control('is-prev', 'Imagem anterior', '‹', () => select(current - 1));
      control('is-next', 'Próxima imagem', '›', () => select(current + 1));
      const pause = control('is-pause', 'Pausar a troca automática', '❚❚', () => {
        paused = !paused;
        pause.textContent = paused ? '▶' : '❚❚';
        pause.title = paused ? 'Retomar a troca automática' : 'Pausar a troca automática';
      });
      control('is-more', 'Ver todas', '⋯', () => { showAll = !showAll; renderThumbs(); });
      stage.appendChild(credit);
      container.appendChild(stage);
      container.appendChild(caption);
      container.appendChild(strip);

      let beats = [];
      let topic = '';
      let active = 0;
      let current = -1;
      let token = 0;
      let paused = false;
      let showAll = false;
      let spoken = [];
      let changedAt = 0;
      let planKey = '';
      const segments = new Map();

      const describe = (beat) => {
        caption.textContent = beat.caption;
        credit.textContent = [beat.source, beat.license].filter(Boolean).join(' · ');
      };

      const renderThumbs = () => {
        clear(strip);
        strip.hidden = !showAll || beats.length < 2;
        if (strip.hidden) return;
        beats.forEach((beat, index) => {
          const button = create(documentForPanel, 'button', `kui-viewer-thumb${index === current ? ' is-current' : ''}`);
          button.type = 'button';
          button.title = beat.caption;
          button.setAttribute?.('aria-label', beat.caption || 'Imagem');
          const thumb = create(documentForPanel, 'img');
          thumb.src = beat.src;
          thumb.alt = '';
          thumb.loading = 'lazy';
          button.appendChild(thumb);
          button.addEventListener?.('click', () => select(index));
          strip.appendChild(button);
        });
      };

      // nenhuma imagem carregou: aviso em vez de painel escuro e vazio (revisão de design, 2026-11-07)
      const noImage = () => {
        skeleton.hidden = true;
        if (!stage.querySelector?.('.kui-gallery-fallback')) stage.appendChild(create(documentForPanel, 'span', 'kui-gallery-fallback', 'Imagem indisponível'));
      };

      const select = async (index) => {
        if (index < 0 || index >= beats.length || index === current) return;
        const beat = beats[index];
        const mine = ++token;
        if (current < 0) skeleton.hidden = false;
        const loaded = await loadImage(beat.src, { imageFactory });
        if (mine !== token) return;
        skeleton.hidden = true;
        if (!loaded) {
          if (index + 1 < beats.length) select(index + 1);
          else noImage();
          return;
        }
        current = index;
        changedAt = now();
        spoken = [];
        const next = layers[1 - active];
        next.alt = beat.caption;
        next.src = loaded;
        setVisible(next, true);
        setVisible(layers[active], false);
        active = 1 - active;
        describe(beat);
        renderThumbs();
      };

      const onSpoken = (event) => {
        const detail = event?.detail || {};
        // A fala é guardada mesmo antes das imagens chegarem: o plano que chega no meio da história já começa no
        // momento que ela está contando.
        segments.set(detail.id || 'atual', String(detail.text || ''));
        if (segments.size > 8) segments.delete(segments.keys().next().value);
        spoken = words([...segments.values()].join(' ')).slice(-SPOKEN_WINDOW_WORDS);
        if (paused || !beats.length || current < 0) return;
        const target = nextSpokenBeat(beats, current, spoken);
        if (target >= 0) { segments.clear(); select(target); return; }
        if (now() - changedAt >= BEAT_FALLBACK_MS && current + 1 < beats.length) select(current + 1);
      };
      view.addEventListener?.('kira:spoken', onSpoken);
      // sem aviso de fala (celular, janela extra): a história anda sozinha a cada BEAT_FALLBACK_MS
      const ticker = view.setInterval?.(() => {
        if (!paused && current >= 0 && current + 1 < beats.length && now() - changedAt >= BEAT_FALLBACK_MS) select(current + 1);
      }, 2000);
      ticker?.unref?.(); // no Node (testes) o relógio não segura o processo; no navegador não muda nada

      const render = (props = {}) => {
        const nextTopic = safeText(props.query);
        if (nextTopic && nextTopic !== topic) {
          topic = nextTopic;
          beats = [];
          current = -1;
          paused = false;
          segments.clear();
        }
        const parse = (beat) => ({
          id: safeText(beat?.id), src: safeImage(beat?.imageUrl), caption: shortCaption(beat?.caption),
          source: safeText(beat?.source), license: safeText(beat?.license),
          keywords: (Array.isArray(beat?.keywords) ? beat.keywords : []).map((key) => fold(key)).filter(Boolean),
        });
        if (!Array.isArray(props.beats)) {
          const single = parse({ imageUrl: props.imageUrl || props.image, caption: props.caption || props.title, source: props.source, license: props.license });
          if (!single.src) { if (!beats.length) noImage(); return; } // sem imagem válida: aviso, não painel escuro
          let known = beats.findIndex((beat) => beat.src === single.src);
          if (known < 0) { beats.push(single); known = beats.length - 1; }
          select(known);
          return;
        }
        const parsed = props.beats.map(parse).filter((beat) => beat.src);
        if (!parsed.length && !beats.length) noImage(); // nenhuma imagem válida: aviso, não o 'carregando' eterno
        const key = parsed.map((beat) => beat.src).join('|');
        if (parsed.length && key !== planKey) {
          // Plano novo: começa já no momento que a Kira está contando (ou no primeiro), sem esperar palavra-chave.
          planKey = key;
          beats = parsed;
          const heardNow = nextSpokenBeat(beats, -1, spoken);
          current = -1;
          select(heardNow >= 0 ? heardNow : 0);
          return;
        }
        if (!beats.length) return;
        const wanted = beats.findIndex((beat) => beat.id && beat.id === safeText(props.activeBeat));
        const index = wanted >= 0 && !(paused && current >= 0) ? wanted : Math.max(current, 0);
        if (index !== current) select(index);
        else renderThumbs();
      };

      render(context.props);
      applyViewerVariation(container, context.props, view);
      container.__kuiViewerPanel = { update: render };
      return () => {
        token += 1;
        view.removeEventListener?.('kira:spoken', onSpoken);
        view.clearInterval?.(ticker);
        delete container.__kuiViewerPanel;
      };
    },
    update(container, props) { container.__kuiViewerPanel?.update?.(props); },
  };
}
