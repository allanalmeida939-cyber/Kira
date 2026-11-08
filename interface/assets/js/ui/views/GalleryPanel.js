import { clear, create, safeImage, safeText } from './panelDom.js';

function galleryItems(props = {}) {
  const source = Array.isArray(props.items) ? props.items : [];
  return source.slice(0, 24).map((item, index) => {
    const imageUrl = safeImage(item?.imageUrl || item?.image || item?.src);
    if (!imageUrl) return null;
    return {
      imageUrl,
      title: safeText(item?.title, `Imagem ${index + 1}`),
      alt: safeText(item?.alt || item?.title, `Imagem ${index + 1}`),
      source: safeText(item?.source, 'Fonte indisponível'),
      license: safeText(item?.license, 'Licença não informada'),
    };
  }).filter(Boolean);
}

function moveIndex(state, delta) {
  if (!state.items.length) return;
  state.pauseUntil = Date.now() + 30000;
  state.autoHighlight = true;
  state.currentIndex = (state.currentIndex + delta + state.items.length) % state.items.length;
  state.lightbox = true;
  state.render();
}

function renderImage(documentRef, item, className) {
  const image = create(documentRef, 'img', className);
  image.loading = 'lazy';
  image.alt = item.alt;
  image.src = item.imageUrl;
  image.addEventListener?.('error', () => {
    image.removeAttribute?.('src');
    image.alt = 'Imagem indisponível';
    image.hidden = true;
    const fallback = create(documentRef, 'span', 'kui-gallery-fallback', 'Imagem indisponível');
    image.parentNode?.appendChild?.(fallback);
  }, { once: true });
  return image;
}

export function createGalleryPanel({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  return {
    mount(container, context) {
      const documentForPanel = container.ownerDocument || documentRef;
      const timerApi = windowRef || globalThis;
      const state = { props: context.props || {}, items: [], currentIndex: 0, lightbox: false, autoHighlight: false, visible: true, pauseUntil: 0, timer: null, render: () => {} };
      const root = create(documentForPanel, 'div', 'kui-gallery-panel');
      container.appendChild(root);
      const markInteraction = () => { state.pauseUntil = Date.now() + 30000; state.autoHighlight = true; };
      /** Auto-cue is intentionally local so a future media cue provider can replace this scheduler. */
      const scheduleAutoCue = () => {
        if (state.timer == null && state.visible) {
          state.timer = (timerApi.setInterval || setInterval)(() => {
            if (!state.visible || state.items.length < 2 || Date.now() < state.pauseUntil) return;
            state.currentIndex = (state.currentIndex + 1) % state.items.length;
            state.autoHighlight = true;
            state.render();
          }, 6000);
        }
      };
      const stopAutoCue = () => {
        if (state.timer != null) (timerApi.clearInterval || clearInterval)(state.timer);
        state.timer = null;
      };
      state.render = () => {
        state.items = galleryItems(state.props);
        if (state.currentIndex >= state.items.length) state.currentIndex = Math.max(0, state.items.length - 1);
        clear(root);
        const query = safeText(state.props.query);
        if (query) root.appendChild(create(documentForPanel, 'p', 'kui-gallery-query', `Pesquisa: ${query}`));
        const grid = create(documentForPanel, 'div', 'kui-gallery-grid');
        state.items.forEach((item, index) => {
          const card = create(documentForPanel, 'button', `kui-gallery-card${index === state.currentIndex && (state.lightbox || state.autoHighlight) ? ' is-current' : ''}`);
          card.type = 'button';
          card.dataset.kuiGalleryIndex = String(index);
          card.appendChild(renderImage(documentForPanel, item, 'kui-gallery-image'));
          const caption = create(documentForPanel, 'span', 'kui-gallery-caption');
          caption.appendChild(create(documentForPanel, 'strong', 'kui-gallery-title', item.title));
          caption.appendChild(create(documentForPanel, 'span', 'kui-gallery-source', `Fonte: ${item.source} · Licença: ${item.license}`));
          card.appendChild(caption);
          card.addEventListener?.('click', () => { markInteraction(); state.currentIndex = index; state.lightbox = true; state.render(); });
          grid.appendChild(card);
        });
        root.appendChild(grid);
        const lightbox = create(documentForPanel, 'div', 'kui-gallery-lightbox');
        lightbox.hidden = !state.lightbox || !state.items.length;
        const previous = create(documentForPanel, 'button', 'kui-panel-button kui-gallery-previous', '‹');
        const next = create(documentForPanel, 'button', 'kui-panel-button kui-gallery-next', '›');
        previous.type = next.type = 'button';
        previous.setAttribute('aria-label', 'Imagem anterior');
        next.setAttribute('aria-label', 'Próxima imagem');
        previous.addEventListener?.('click', () => moveIndex(state, -1));
        next.addEventListener?.('click', () => moveIndex(state, 1));
        lightbox.appendChild(previous);
        if (state.items.length) {
          const current = state.items[state.currentIndex];
          lightbox.appendChild(renderImage(documentForPanel, current, 'kui-gallery-lightbox-image'));
          const caption = create(documentForPanel, 'p', 'kui-gallery-lightbox-caption', `${current.title} · Fonte: ${current.source} · Licença: ${current.license}`);
          lightbox.appendChild(caption);
        }
        lightbox.appendChild(next);
        root.appendChild(lightbox);
      };
      const onKeyDown = (event) => {
        if (!state.lightbox) return;
        if (event.key === 'ArrowLeft') moveIndex(state, -1);
        if (event.key === 'ArrowRight') moveIndex(state, 1);
        if (event.key === 'Escape') { markInteraction(); state.lightbox = false; state.render(); }
      };
      const onCue = (action) => {
        if (action?.panelId !== context.id || !Number.isInteger(action.index) || action.index < 0 || action.index >= state.items.length) return;
        markInteraction();
        state.currentIndex = action.index;
        state.autoHighlight = true;
        state.lightbox = true;
        state.render();
      };
      documentForPanel?.addEventListener?.('keydown', onKeyDown);
      const unsubscribe = context.bus?.on?.('action:media_cue', onCue) || (() => {});
      state.render();
      scheduleAutoCue();
      container.__kuiGalleryPanel = {
        update: (props) => { state.props = props || {}; state.render(); },
        visibilityChange: (visible) => { state.visible = Boolean(visible); if (state.visible) scheduleAutoCue(); else stopAutoCue(); },
      };
      return () => {
        stopAutoCue();
        unsubscribe();
        documentForPanel?.removeEventListener?.('keydown', onKeyDown);
        delete container.__kuiGalleryPanel;
      };
    },
    update(container, props) { container.__kuiGalleryPanel?.update?.(props); },
    visibilityChange(container, visible) { container.__kuiGalleryPanel?.visibilityChange?.(visible); },
  };
}
