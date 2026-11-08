import { clear, create, safeText } from './panelDom.js';

function entries(props = {}) {
  const value = Array.isArray(props.items) ? props.items : Array.isArray(props.logs) ? props.logs : [];
  return value.slice(-100);
}

function entryText(item) {
  const query = safeText(item?.query, 'consulta sem texto');
  const types = Array.isArray(item?.types) ? item.types.join(', ') : safeText(item?.types, 'geral');
  const time = safeText(item?.time || item?.timestamp, 'hora indisponível');
  const result = item?.ok === false ? 'falha' : 'ok';
  return `${query} · ${types} · ${time} · ${result}`;
}

export function createSearchLogPanel({ documentRef = globalThis.document } = {}) {
  return {
    mount(container, context) {
      const documentForPanel = container.ownerDocument || documentRef;
      const state = { props: context.props || {}, items: entries(context.props) };
      const render = () => {
        clear(container);
        container.appendChild(create(documentForPanel, 'p', 'kui-search-log-heading', 'Histórico desta sessão'));
        const feed = create(documentForPanel, 'ol', 'kui-search-log-feed');
        state.items.forEach((item) => feed.appendChild(create(documentForPanel, 'li', item?.ok === false ? 'is-fail' : 'is-ok', entryText(item))));
        container.appendChild(feed);
      };
      render();
      container.__kuiSearchLogPanel = {
        update: (props = {}) => {
          if (Array.isArray(props.items)) state.items = props.items.slice(-100);
          if (props.append) state.items = [...state.items, ...(Array.isArray(props.append) ? props.append : [props.append])].slice(-100);
          state.props = props;
          render();
        },
      };
      return () => { delete container.__kuiSearchLogPanel; };
    },
    update(container, props) { container.__kuiSearchLogPanel?.update?.(props); },
  };
}
