import { clear, create, safeImage, safeText } from './panelDom.js';

function number(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatPercent(value) {
  return value == null || !Number.isFinite(Number(value)) ? '—' : `${Number(value).toFixed(2).replace('.', ',')}%`;
}

function initials(name) {
  return safeText(name, '?').split(/\s+/).filter(Boolean).slice(0, 2).map((word) => word[0]).join('').toUpperCase();
}

function photo(documentRef, name, photoUrl) {
  const holder = create(documentRef, 'span', 'kui-election-photo');
  const src = safeImage(photoUrl);
  const fallback = () => { holder.textContent = initials(name); holder.className += ' is-fallback'; };
  if (!src) { fallback(); return holder; }
  const image = create(documentRef, 'img', 'kui-election-img');
  image.alt = safeText(name);
  image.loading = 'lazy';
  image.addEventListener?.('error', () => { image.remove?.(); fallback(); });
  image.src = src;
  holder.appendChild(image);
  return holder;
}

function bar(documentRef, percent) {
  const track = create(documentRef, 'span', 'kui-bar-track');
  const fill = create(documentRef, 'span', 'kui-bar-fill');
  fill.style.width = `${Math.min(100, Math.max(0, number(percent)))}%`;
  track.appendChild(fill);
  return track;
}

export function createElectionPanel({ documentRef = globalThis.document } = {}) {
  return {
    mount(container, context) {
      const documentForPanel = container.ownerDocument || documentRef;
      const render = (props = {}) => {
        clear(container);
        container.appendChild(create(documentForPanel, 'h3', 'kui-election-title', safeText(props.title, 'Eleição')));
        const kind = [safeText(props.kind), safeText(props.status)].filter(Boolean).join(' · ');
        if (kind) container.appendChild(create(documentForPanel, 'p', 'kui-election-kind', kind));
        const incoherent = safeText(props.summary);
        if (incoherent) container.appendChild(create(documentForPanel, 'p', 'kui-election-warning', `${incoherent}. Sem gráfico até a fonte corrigir.`));
        const counted = create(documentForPanel, 'div', 'kui-election-counted');
        counted.appendChild(create(documentForPanel, 'span', '', `${formatPercent(props.apurado)} das urnas apuradas`));
        counted.appendChild(bar(documentForPanel, props.apurado));
        container.appendChild(counted);
        const items = (Array.isArray(props.items) ? props.items : []).slice(0, 8);
        const topVotes = Math.max(0, ...items.map((item) => number(item?.votes)));
        const list = create(documentForPanel, 'ol', 'kui-election-list');
        items.forEach((item, index) => {
          const row = create(documentForPanel, 'li', `kui-election-row${index === 0 && topVotes > 0 ? ' is-leader' : ''}`);
          row.appendChild(photo(documentForPanel, item?.name, item?.photoUrl));
          const body = create(documentForPanel, 'div', 'kui-election-body');
          const head = create(documentForPanel, 'div', 'kui-election-head');
          head.appendChild(create(documentForPanel, 'strong', 'kui-election-name', safeText(item?.name, 'Candidato')));
          head.appendChild(create(documentForPanel, 'span', 'kui-election-pct', formatPercent(item?.pct)));
          body.appendChild(head);
          if (!incoherent && item?.pct != null) body.appendChild(bar(documentForPanel, item.pct));
          const meta = [safeText(item?.party), item?.number ? `nº ${safeText(item.number)}` : '', `${number(item?.votes).toLocaleString('pt-BR')} votos`, safeText(item?.status)].filter(Boolean).join(' · ');
          body.appendChild(create(documentForPanel, 'span', 'kui-election-meta', meta));
          row.appendChild(body);
          list.appendChild(row);
        });
        container.appendChild(list);
        container.appendChild(create(documentForPanel, 'p', 'kui-panel-footer', `atualizado ${safeText(props.updatedAt, '—')} · consultado ${safeText(props.date, '—')} · fonte: ${safeText(props.source, 'TSE')}`));
      };
      render(context.props);
      container.__kuiElectionPanel = { update: render };
      return () => { delete container.__kuiElectionPanel; };
    },
    update(container, props) { container.__kuiElectionPanel?.update?.(props); },
  };
}
