import { clear, create, safeText } from './panelDom.js';
import { COMPONENTS, openExternal } from './presentation/components.js';
import { stageCardWidth, stageLayout } from './presentation/stageLayout.js';

export const STATUS_LABELS = Object.freeze({
  preparing_query: 'Preparando a pesquisa',
  researching: 'Pesquisando',
  collecting_media: 'Reunindo imagens',
  organizing: 'Organizando',
  ready: 'Pronto',
  partial: 'Parcial: algumas partes não vieram',
  error: 'Não consegui obter os dados',
  cancelled: 'Cancelado',
});
const BUSY = new Set(['preparing_query', 'researching', 'collecting_media', 'organizing']);
// Tempo para tirar a classe de entrada (a animação mais longa: 720 ms x 1,45 na velocidade 'Suave').
const ENTER_CLEAR_MS = 1500;
// Modo palco (briefing): depois que a Kira chega na última seção e fica este tempo sem falar, tudo se junta no resumo.
export const STAGE_SUMMARY_QUIET_MS = 4000;
const STAGE_FALLBACK_MS = 15000;
// Palavras que, ditas pela Kira, trazem a seção para o palco (além do próprio título da seção).
const STAGE_WORDS = Object.freeze({
  weather: ['clima', 'tempo', 'temperatura', 'chuva', 'graus', 'previsao'],
  news: ['noticia', 'noticias', 'manchete', 'manchetes'],
  agenda: ['agenda', 'compromisso', 'compromissos', 'reuniao'],
  email: ['email', 'e-mail', 'emails', 'mensagens'],
  health: ['saude', 'passos', 'sono'],
});
const fold = (text) => String(text || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
const stageWords = (section) => {
  const heading = fold(section.heading).split(/[^a-z0-9-]+/).filter((w) => w.length > 3);
  const stems = heading.map((w) => w.replace(/s$/, ''));
  return [...new Set([...heading, ...stems, ...(STAGE_WORDS[section.type] || [])])];
};

/**
 * Painel de apresentação: estado isolado por presentationId, patch por seção (nunca recria o que não mudou),
 * eventos de requestId antigo ignorados e limpeza de todos os componentes ao fechar.
 */
export function createPresentationPanel({ documentRef = globalThis.document, open = openExternal, view = globalThis.window } = {}) {
  return {
    mount(container, context) {
      const doc = container.ownerDocument || documentRef;
      clear(container);
      const head = create(doc, 'div', 'kui-pres-head');
      const status = create(doc, 'p', 'kui-pres-status');
      status.setAttribute?.('role', 'status');
      const nav = create(doc, 'div', 'kui-pres-nav');
      nav.setAttribute?.('role', 'tablist');
      head.appendChild(status);
      head.appendChild(nav);
      const body = create(doc, 'div', 'kui-pres-body');
      container.appendChild(head);
      container.appendChild(body);

      const state = { presentationId: '', requestId: '', order: [], sections: new Map(), sources: [], active: '', stage: false, summary: false, revealed: [] };
      let summaryTimer = null;
      let fallbackTimer = null;
      let frame = 0;
      const Observer = view?.ResizeObserver;
      // Imagem que chega, notícia que entra ou janela que muda de tamanho: o palco se reorganiza sozinho.
      const observer = typeof Observer === 'function' ? new Observer(() => scheduleStage()) : null;
      observer?.observe?.(body);
      const clearTimers = () => {
        if (summaryTimer) view?.clearTimeout?.(summaryTimer);
        if (fallbackTimer) view?.clearTimeout?.(fallbackTimer);
        summaryTimer = fallbackTimer = null;
      };
      const readyIds = () => state.order.filter((id) => state.sections.get(id).section.status === 'ready');

      const isShown = (record, id) => (!state.summary ? state.revealed.includes(id) : record.section.status === 'ready');

      // Entrada com movimento (aba Aparência: do orbe, subindo, deslizando ou só aparecendo). O cartão espera escondido
      // até o quadro em que o lugar dele é calculado; aí sai do centro da tela (o orbe) até o lugar.
      const enter = (record, spot, cardWidth, box) => {
        record.pendingEnter = false;
        record.entered = true;
        if (spot && box && view?.innerWidth) {
          const height = Math.max(1, record.root.offsetHeight || 0);
          const centerX = box.left + spot.x + (cardWidth * spot.scale) / 2;
          const centerY = box.top + spot.y + (height * spot.scale) / 2;
          record.root.style.setProperty?.('--enter-x', `${Math.round(view.innerWidth / 2 - centerX)}px`);
          record.root.style.setProperty?.('--enter-y', `${Math.round(view.innerHeight / 2 - centerY)}px`);
        }
        record.root.classList?.remove?.('is-stage-hidden');
        if (!spot) return;
        record.root.classList?.add?.('is-stage-enter');
        view?.setTimeout?.(() => record.root.classList?.remove?.('is-stage-enter'), ENTER_CLEAR_MS);
      };

      // Palco medido: todo cartão com a largura da coluna principal, posto no lugar só com mover + encolher
      // (stageLayout). Antes era uma grade que dependia do conteúdo: as notícias cobriam o clima e passavam da janela.
      const placeStage = () => {
        frame = 0;
        const width = body.clientWidth;
        const height = body.clientHeight;
        if (!state.stage || !body.style || !(width > 0) || !(height > 0)) {
          state.sections.forEach((record) => { if (record.pendingEnter) enter(record, null); });
          return;
        }
        const cardWidth = stageCardWidth(width);
        const shown = [];
        const waiting = [];
        state.order.forEach((id) => {
          const record = state.sections.get(id);
          if (record.root.style.width !== `${cardWidth}px`) record.root.style.width = `${cardWidth}px`;
          (isShown(record, id) ? shown : waiting).push({ id, height: Math.max(1, record.root.offsetHeight || 0) });
        });
        const spots = stageLayout({ width, height, cardWidth, items: shown, active: state.active, summary: state.summary });
        // Quem ainda não apareceu já espera onde vai entrar: surge ali, sem voar do canto.
        waiting.forEach((item) => spots.set(item.id, stageLayout({ width, height, cardWidth, items: [...shown, item], active: item.id }).get(item.id)));
        const box = body.getBoundingClientRect?.();
        spots.forEach((spot, id) => {
          if (!spot) return;
          const record = state.sections.get(id);
          record.root.style.transform = `translate3d(${spot.x.toFixed(1)}px, ${spot.y.toFixed(1)}px, 0) scale(${spot.scale.toFixed(4)})`;
          if (record.pendingEnter) enter(record, spot, cardWidth, box);
        });
      };
      function scheduleStage() {
        if (!state.stage || frame) return;
        frame = view?.requestAnimationFrame?.(placeStage) || 0;
        if (!frame) placeStage();
      }

      // Palco: a seção que a Kira está falando aparece grande; as anteriores encolhem para o lado.
      const layoutStage = () => {
        if (!state.stage) return;
        state.sections.forEach((record, id) => {
          const shown = isShown(record, id);
          if (shown && !state.summary && !record.entered && record.root.classList?.contains?.('is-stage-hidden')) record.pendingEnter = true;
          record.root.classList?.toggle?.('is-stage-hidden', !shown || Boolean(record.pendingEnter));
          record.root.classList?.toggle?.('is-stage-main', !state.summary && id === state.active);
          record.root.classList?.toggle?.('is-stage-side', !state.summary && shown && id !== state.active);
        });
        scheduleStage();
      };
      const showSummary = () => {
        if (!state.stage || state.summary) return;
        clearTimers();
        state.summary = true;
        body.classList?.add?.('is-summary');
        layoutStage();
      };
      const armSummary = () => {
        const ready = readyIds();
        if (!state.stage || state.summary || !ready.length || state.active !== ready[ready.length - 1]) return;
        if (summaryTimer) view?.clearTimeout?.(summaryTimer);
        summaryTimer = view?.setTimeout?.(showSummary, STAGE_SUMMARY_QUIET_MS);
      };
      // O palco segue a fala: se a Kira chama a ferramenta da seção tarde (ou tudo de uma vez no fim), a seção
      // aparece quando ela começa a falar dela.
      const onSpoken = (event) => {
        if (state.stage && !state.summary) {
          const words = new Set(fold(event?.detail?.text).split(/[^a-z0-9-]+/).filter(Boolean));
          const current = state.order.indexOf(state.active);
          const next = state.order.find((id, index) => {
            const record = state.sections.get(id);
            return index > current && record.section.status === 'ready' && stageWords(record.section).some((w) => words.has(w));
          });
          if (next) setActive(next);
        }
        armSummary();
      };

      const describeSection = (record) => {
        record.heading.textContent = record.section.heading || '';
        record.root.dataset.status = record.section.status || 'pending';
        record.note.textContent = record.section.status === 'unavailable' ? `Indisponível: ${safeText(record.section.reason, 'a fonte não respondeu')}` : '';
        if (record.section.status === 'pending' && !record.note.ownerDocument?.createElement) record.note.textContent = 'Carregando…';
        else if (record.section.status === 'pending') { // mini Thinking Orb no lugar do "Carregando…"
          const orb = record.note.ownerDocument.createElement('kira-orb');
          orb.setAttribute('state', 'buscando'); orb.setAttribute('size', '20'); orb.setAttribute('label', 'Buscando');
          record.note.append(orb);
        }
        record.tab.textContent = record.section.heading || record.section.type;
      };

      const renderSection = (record) => {
        const { section } = record;
        describeSection(record);
        if (section.status !== 'ready') return;
        const data = section.type === 'sources' ? { items: state.sources, ...(section.data || {}) } : (section.data || {});
        if (record.instance) { record.instance.update(data); return; }
        const component = COMPONENTS.get(section.type);
        if (!component) { record.note.textContent = 'Este tipo de conteúdo ainda não está disponível.'; return; }
        record.instance = component.mount(record.content, data, { documentRef: doc, open });
      };

      const setActive = (id) => {
        if (!state.sections.has(id)) return;
        state.active = id;
        if (state.stage && !state.revealed.includes(id) && state.sections.get(id).section.status !== 'unavailable') state.revealed.push(id);
        if (fallbackTimer) { view?.clearTimeout?.(fallbackTimer); fallbackTimer = null; }
        state.sections.forEach((record, key) => {
          const on = key === id;
          record.root.classList?.toggle?.('is-active', on);
          record.tab.setAttribute?.('aria-selected', String(on));
          record.tab.className = `kui-pres-tab${on ? ' is-active' : ''}`;
        });
        layoutStage();
        armSummary();
        if (!state.stage) state.sections.get(id).root.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
      };

      const addSection = (section) => {
        const root = create(doc, 'section', 'kui-pres-section');
        root.dataset.type = section.type;
        const heading = create(doc, 'h3', 'kui-pres-heading');
        const note = create(doc, 'p', 'kui-pres-muted');
        const content = create(doc, 'div', 'kui-pres-content');
        root.appendChild(heading);
        root.appendChild(note);
        root.appendChild(content);
        body.appendChild(root);
        observer?.observe?.(root);
        const tab = create(doc, 'button', 'kui-pres-tab');
        tab.type = 'button';
        tab.setAttribute?.('role', 'tab');
        tab.addEventListener?.('click', () => setActive(section.id));
        tab.addEventListener?.('keydown', (event) => {
          const index = state.order.indexOf(section.id);
          const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
          const next = state.order[index + step];
          if (step && next) { setActive(next); state.sections.get(next).tab.focus?.(); }
        });
        nav.appendChild(tab);
        const record = { section: { ...section }, root, heading, note, content, tab, instance: null };
        state.sections.set(section.id, record);
        state.order.push(section.id);
        renderSection(record);
      };

      const reset = () => {
        clearTimers();
        if (frame) view?.cancelAnimationFrame?.(frame);
        frame = 0;
        state.sections.forEach((record) => observer?.unobserve?.(record.root));
        state.stage = state.summary = false;
        container.closest?.('.kui-window')?.classList?.remove?.('is-bare');
        state.revealed = [];
        body.classList?.remove?.('is-stage', 'is-summary');
        state.sections.forEach((record) => record.instance?.unmount?.());
        state.sections.clear();
        state.order = [];
        state.sources = [];
        state.active = '';
        clear(body);
        clear(nav);
      };

      const setStatus = (value) => {
        status.textContent = STATUS_LABELS[value] || '';
        status.dataset.state = value || '';
        status.classList?.toggle?.('is-busy', BUSY.has(value));
      };

      const apply = (action) => {
        if (!action || typeof action !== 'object') return;
        if (action.type === 'present') {
          if (state.presentationId === action.presentationId && state.requestId === action.requestId) return;
          reset();
          state.presentationId = action.presentationId;
          state.requestId = action.requestId;
          state.sources = action.sources || [];
          state.stage = action.mode === 'stage';
          body.classList?.toggle?.('is-stage', state.stage);
          // Briefing em palco sem moldura nem cabeçalho de janela: cada seção é um cartão solto (o clima aparece
          // sozinho, encolhe para o canto superior esquerdo quando entram as notícias).
          container.closest?.('.kui-window')?.classList?.toggle?.('is-bare', state.stage);
          nav.hidden = state.stage;
          (action.sections || []).forEach(addSection);
          layoutStage();
          // Se a Kira não chegar a narrar seção por seção, mostra o resumo assim mesmo.
          if (state.stage) fallbackTimer = view?.setTimeout?.(() => { if (!state.active) showSummary(); }, STAGE_FALLBACK_MS);
          setStatus(action.status);
          if (action.activeSectionId) setActive(action.activeSectionId);
          return;
        }
        if (action.type !== 'present_patch' || action.requestId !== state.requestId) return;
        if (action.sources) state.sources = action.sources;
        (action.sections || []).forEach((patch) => {
          const record = state.sections.get(patch.id);
          if (!record) { if (patch.type) addSection(patch); return; }
          record.section = { ...record.section, ...patch };
          renderSection(record);
        });
        if (action.sources) state.sections.forEach((record) => { if (record.section.type === 'sources') renderSection(record); });
        if (action.status) setStatus(action.status);
        if (action.activeSectionId) setActive(action.activeSectionId);
        if (action.stage === 'summary') showSummary();
        layoutStage();
      };

      apply(context.props?.presentation);
      view?.addEventListener?.('kira:spoken', onSpoken);
      container.__kuiPresentation = { apply, state };
      return () => { view?.removeEventListener?.('kira:spoken', onSpoken); reset(); observer?.disconnect?.(); delete container.__kuiPresentation; };
    },
    update(container, props) { container.__kuiPresentation?.apply?.(props?.presentation); },
  };
}
