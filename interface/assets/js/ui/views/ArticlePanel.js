import { clear, create, safeHttps, safeImage, safeText } from './panelDom.js';

export function createArticlePanel({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  return {
    mount(container, context) {
      const documentForPanel = container.ownerDocument || documentRef;
      const render = (props = {}) => {
        clear(container);
        const title = create(documentForPanel, 'h3', 'kui-article-title', safeText(props.title, 'Artigo'));
        container.appendChild(title);
        const imageUrl = safeImage(props.thumbnail || props.imageUrl || props.image);
        if (imageUrl) {
          const image = create(documentForPanel, 'img', 'kui-article-thumbnail');
          image.src = imageUrl;
          image.alt = safeText(props.title, 'Miniatura do artigo');
          image.loading = 'lazy';
          image.addEventListener?.('error', () => { image.removeAttribute?.('src'); image.hidden = true; });
          container.appendChild(image);
        }
        container.appendChild(create(documentForPanel, 'p', 'kui-article-excerpt', safeText(props.excerpt || props.description || props.content, 'Extrato indisponível.')));
        const source = create(documentForPanel, 'span', 'kui-article-source', `Fonte: ${safeText(props.source, 'Fonte indisponível')}`);
        container.appendChild(source);
        const sourceUrl = safeHttps(props.url || props.href || props.link);
        const button = create(documentForPanel, 'button', 'kui-panel-button', 'Abrir fonte');
        button.type = 'button';
        button.disabled = !sourceUrl;
        button.title = sourceUrl ? 'Abrir fonte no navegador' : 'indisponível nesta versão';
        button.addEventListener?.('click', () => { if (sourceUrl) windowRef?.open?.(sourceUrl, '_blank', 'noopener,noreferrer'); });
        container.appendChild(button);
      };
      render(context.props);
      container.__kuiArticlePanel = { update: render };
      return () => { delete container.__kuiArticlePanel; };
    },
    update(container, props) { container.__kuiArticlePanel?.update?.(props); },
  };
}
