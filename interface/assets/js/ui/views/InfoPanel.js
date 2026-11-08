import { clear, create, safeHttps, safeImage, safeText } from './panelDom.js';

export function createInfoPanel({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  return {
    mount(container, context) {
      const documentForPanel = container.ownerDocument || documentRef;
      const render = (props = {}) => {
        clear(container);
        container.appendChild(create(documentForPanel, 'h3', 'kui-info-title', safeText(props.title, 'Pesquisa')));
        const imageUrl = safeImage(props.imageUrl || props.image);
        if (imageUrl) {
          const image = create(documentForPanel, 'img', 'kui-article-thumbnail');
          image.alt = safeText(props.title, 'Imagem');
          image.loading = 'lazy';
          image.addEventListener?.('error', () => { image.remove?.(); });
          image.src = imageUrl;
          container.appendChild(image);
        }
        if (props.summary) container.appendChild(create(documentForPanel, 'p', 'kui-info-summary', safeText(props.summary)));
        const facts = Array.isArray(props.facts) ? props.facts.slice(0, 8) : [];
        if (facts.length) {
          const list = create(documentForPanel, 'dl', 'kui-info-facts');
          facts.forEach((fact) => {
            list.appendChild(create(documentForPanel, 'dt', '', safeText(fact?.label)));
            list.appendChild(create(documentForPanel, 'dd', '', safeText(fact?.value)));
          });
          container.appendChild(list);
        }
        const sources = (Array.isArray(props.sources) ? props.sources : []).slice(0, 5);
        if (sources.length) {
          container.appendChild(create(documentForPanel, 'p', 'kui-info-heading', 'Fontes'));
          const links = create(documentForPanel, 'ul', 'kui-info-sources');
          sources.forEach((source) => {
            const url = safeHttps(source?.url || source?.href);
            const item = create(documentForPanel, 'li');
            const button = create(documentForPanel, 'button', 'kui-info-source', safeText(source?.title || source?.source, 'Fonte'));
            button.type = 'button';
            button.disabled = !url;
            button.title = url ? safeText(source?.source, 'Abrir no navegador') : 'indisponível';
            button.addEventListener?.('click', () => { if (url) windowRef?.open?.(url, '_blank', 'noopener,noreferrer'); });
            item.appendChild(button);
            links.appendChild(item);
          });
          container.appendChild(links);
        }
        if (props.text) container.appendChild(create(documentForPanel, 'p', 'kui-panel-footer is-warning', safeText(props.text)));
        if (props.source) container.appendChild(create(documentForPanel, 'p', 'kui-panel-footer', `fonte: ${safeText(props.source)}`));
      };
      render(context.props);
      container.__kuiInfoPanel = { update: render };
      return () => { delete container.__kuiInfoPanel; };
    },
    update(container, props) { container.__kuiInfoPanel?.update?.(props); },
  };
}
