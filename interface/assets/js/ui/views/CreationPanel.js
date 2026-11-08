import { clear, create, safeText } from './panelDom.js';

// Painel "Criação": só a imagem gerada pela Kira, grande. Mandar no WhatsApp ou gerar outra é por voz.

export function createCreationPanel() {
  return {
    mount(container, context) {
      const doc = container.ownerDocument;
      container.classList.add('kui-creation-content');
      const render = (props = {}) => {
        clear(container);
        const src = String(props.imageUrl || '');
        if (!src.startsWith('/api/ui/creation/file/')) return;
        const image = create(doc, 'img', 'kui-creation-image');
        image.alt = safeText(props.title, 'Imagem criada');
        image.src = src;
        container.appendChild(image);
      };
      render(context.props || {});
      container.__kuiCreationPanel = { update: render };
      return () => {
        container.classList.remove('kui-creation-content');
        delete container.__kuiCreationPanel;
      };
    },
    update(container, props) { container.__kuiCreationPanel?.update?.(props); },
  };
}
