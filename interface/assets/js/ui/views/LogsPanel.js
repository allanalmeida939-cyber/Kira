import { clear, create, safeText } from './panelDom.js';

function levelOf(item) { return safeText(item?.level || item?.status, 'info').toLowerCase(); }
function logText(item) { return `[${levelOf(item)}] ${safeText(item?.text || item?.message || item?.content, 'evento')}`; }

export function createLogsPanel({ documentRef = globalThis.document, windowRef = globalThis.window, navigatorRef = globalThis.navigator } = {}) {
  return {
    mount(container, context) {
      const documentForPanel = container.ownerDocument || documentRef;
      const state = { props: context.props || {}, logs: Array.isArray(context.props?.logs) ? context.props.logs.slice(-48) : [], filter: 'all' };
      const render = () => {
        clear(container);
        const toolbar = create(documentForPanel, 'div', 'kui-logs-toolbar');
        const select = create(documentForPanel, 'select', 'kui-logs-filter');
        [['all', 'Todos'], ['info', 'Info'], ['warn', 'Avisos'], ['error', 'Erros'], ['tv', 'TV'], ['celular', 'Celular']].forEach(([value, label]) => {
          const option = create(documentForPanel, 'option', '', label); option.value = value; select.appendChild(option);
        });
        select.value = state.filter;
        select.addEventListener?.('change', () => {
          state.filter = select.value;
          // TV e Celular: lê os arquivos logs/TV e logs/Celular (não ficam no feed da tela)
          if (state.filter === 'tv' || state.filter === 'celular') {
            fetch(`/api/logs/canal?nome=${state.filter}`, { cache: 'no-store' }).then((r) => r.json())
              .then((body) => { state.channel = (body?.linhas || []).slice(-200); render(); }).catch(() => { state.channel = []; render(); });
          }
          render();
        });
        const copy = create(documentForPanel, 'button', 'kui-panel-button', 'Copiar');
        copy.type = 'button';
        copy.addEventListener?.('click', () => navigatorRef?.clipboard?.writeText?.(state.logs.map(logText).join('\n')));
        const clearButton = create(documentForPanel, 'button', 'kui-panel-button', 'Limpar');
        clearButton.type = 'button';
        clearButton.addEventListener?.('click', () => { state.logs = []; render(); });
        toolbar.appendChild(select); toolbar.appendChild(copy); toolbar.appendChild(clearButton); container.appendChild(toolbar);
        const feed = create(documentForPanel, 'ol', 'kui-logs-feed');
        if (state.filter === 'tv' || state.filter === 'celular') (state.channel || []).forEach((line) => feed.appendChild(create(documentForPanel, 'li', /\[(WARNING|ERROR)\]/.test(line) ? 'kui-log-warn' : 'kui-log-info', safeText(line))));
        else state.logs.filter((item) => state.filter === 'all' || levelOf(item) === state.filter).forEach((item) => feed.appendChild(create(documentForPanel, 'li', `kui-log-${levelOf(item)}`, logText(item))));
        container.appendChild(feed);
      };
      const append = (action) => { state.logs = [...state.logs, { level: action?.level || 'info', text: action?.text || action?.type }].slice(-48); render(); };
      const unsubscribe = context.bus?.on?.('action', append) || (() => {});
      render();
      container.__kuiLogsPanel = { update: (props = {}) => { if (Array.isArray(props.logs)) state.logs = props.logs.slice(-48); if (props.append) state.logs.push(props.append); render(); } };
      return () => { unsubscribe(); delete container.__kuiLogsPanel; };
    },
    update(container, props) { container.__kuiLogsPanel?.update?.(props); },
  };
}
