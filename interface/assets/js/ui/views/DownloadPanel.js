import { clear, create, safeText } from './panelDom.js';

const STATE_LABELS = Object.freeze({
  confirm: 'Aguardando confirmação',
  downloading: 'Baixando…',
  done: 'Concluído',
  failed: 'Falhou',
  cancelled: 'Cancelado',
});

function post(windowRef, path, body = {}) {
  try {
    return windowRef?.fetch?.(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })?.catch?.(() => {});
  } catch (_) {
    return undefined;
  }
}

export function createDownloadPanel({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  return {
    mount(container, context) {
      const documentForPanel = container.ownerDocument || documentRef;
      const downloadId = String(context.id || '').replace(/^download-/, '');
      const button = (label, onClick) => {
        const node = create(documentForPanel, 'button', 'kui-panel-button', label);
        node.type = 'button';
        node.addEventListener?.('click', onClick);
        return node;
      };
      const render = (props = {}) => {
        clear(container);
        const state = safeText(props.state, 'confirm');
        container.appendChild(create(documentForPanel, 'h3', 'kui-download-name', safeText(props.name, 'arquivo')));
        container.appendChild(create(documentForPanel, 'p', 'kui-download-source', `de ${safeText(props.source, 'site')}`));
        const track = create(documentForPanel, 'span', 'kui-bar-track');
        const fill = create(documentForPanel, 'span', 'kui-bar-fill');
        const total = Number(props.duration) || 0;
        const received = Number(props.value) || 0;
        fill.style.width = state === 'done' ? '100%' : total > 0 ? `${Math.min(100, (received / total) * 100)}%` : state === 'downloading' ? '30%' : '0%';
        track.appendChild(fill);
        container.appendChild(track);
        container.appendChild(create(documentForPanel, 'p', `kui-download-state is-${state}`, `${STATE_LABELS[state] || state}${props.text ? ` · ${safeText(props.text)}` : ''}`));
        const actions = create(documentForPanel, 'div', 'kui-download-actions');
        if (state === 'confirm') actions.appendChild(button('Baixar', () => post(windowRef, '/api/ui/download/confirm', { id: downloadId })));
        if (state === 'confirm' || state === 'downloading') actions.appendChild(button('Cancelar', () => post(windowRef, '/api/ui/download/cancel', { id: downloadId })));
        if (state === 'done') actions.appendChild(button('Abrir pasta', () => post(windowRef, '/api/ui/download/open-folder')));
        container.appendChild(actions);
      };
      render(context.props);
      container.__kuiDownloadPanel = { update: render };
      return () => { delete container.__kuiDownloadPanel; };
    },
    update(container, props) { container.__kuiDownloadPanel?.update?.(props); },
  };
}
