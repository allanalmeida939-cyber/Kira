import { clear, create, safeImage, safeText } from './panelDom.js';

const STATE_LABELS = Object.freeze({ live: 'Ao vivo', paused: 'Intervalo', finished: 'Encerrado', scheduled: 'Agendado' });

function initials(name) {
  return safeText(name, '?').split(/\s+/).filter(Boolean).slice(0, 2).map((word) => word[0]).join('').toUpperCase();
}

function crest(documentRef, name, logoUrl) {
  const holder = create(documentRef, 'span', 'kui-score-crest');
  const src = safeImage(logoUrl);
  const fallback = () => { holder.textContent = initials(name); holder.className += ' is-fallback'; };
  if (!src) { fallback(); return holder; }
  const image = create(documentRef, 'img', 'kui-score-logo');
  image.alt = safeText(name);
  image.loading = 'lazy';
  image.addEventListener?.('error', () => { image.remove?.(); fallback(); });
  image.src = src;
  holder.appendChild(image);
  return holder;
}

function side(documentRef, name, logoUrl) {
  const column = create(documentRef, 'div', 'kui-score-side');
  column.appendChild(crest(documentRef, name, logoUrl));
  column.appendChild(create(documentRef, 'span', 'kui-score-team', safeText(name, '—')));
  return column;
}

function matchBlock(documentRef, match) {
  const state = safeText(match?.state, 'scheduled');
  const block = create(documentRef, 'section', `kui-score-match is-${state}`);
  if (match?.label) block.appendChild(create(documentRef, 'p', 'kui-score-label', safeText(match.label)));
  block.appendChild(create(documentRef, 'p', 'kui-score-league', safeText(match?.league, 'Competição')));
  const row = create(documentRef, 'div', 'kui-score-row');
  row.appendChild(side(documentRef, match?.home, match?.homeLogoUrl));
  const scoreText = state === 'scheduled' ? safeText(match?.time, 'vs') : `${safeText(match?.homeScore, '-')} × ${safeText(match?.awayScore, '-')}`;
  row.appendChild(create(documentRef, 'strong', 'kui-score-value', scoreText));
  row.appendChild(side(documentRef, match?.away, match?.awayLogoUrl));
  block.appendChild(row);
  const detail = [STATE_LABELS[state], state === 'live' ? safeText(match?.minute) : '', state === 'scheduled' ? safeText(match?.date) : ''].filter(Boolean).join(' · ');
  block.appendChild(create(documentRef, 'p', 'kui-score-status', detail));
  if (match?.venue) block.appendChild(create(documentRef, 'p', 'kui-score-venue', safeText(match.venue)));
  return block;
}

export function createScoreboardPanel({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  return {
    mount(container, context) {
      const documentForPanel = container.ownerDocument || documentRef;
      const render = (props = {}) => {
        clear(container);
        const items = Array.isArray(props.items) ? props.items.slice(0, 12) : [];
        if (!items.length || props.state === 'none') {
          container.appendChild(create(documentForPanel, 'p', 'kui-score-empty', safeText(props.text, 'Sem jogo hoje.')));
        }
        items.forEach((item) => container.appendChild(matchBlock(documentForPanel, item)));
        const footer = [props.updatedAt ? `atualizado às ${safeText(props.updatedAt)}` : '', props.source ? `fonte: ${safeText(props.source)}` : ''].filter(Boolean).join(' · ');
        if (footer) container.appendChild(create(documentForPanel, 'p', 'kui-panel-footer', footer));
        if (props.stale === true) container.appendChild(create(documentForPanel, 'p', 'kui-panel-footer is-warning', 'Fonte gratuita: pode estar desatualizado.'));
      };
      render(context.props);
      container.__kuiScoreboardPanel = { update: render };
      return () => {
        delete container.__kuiScoreboardPanel;
        try {
          windowRef?.fetch?.('/api/ui/score/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ panel_id: context.id }) })?.catch?.(() => {});
        } catch (_) { /* parar a atualização é opcional: o backend encerra sozinho */ }
      };
    },
    update(container, props) { container.__kuiScoreboardPanel?.update?.(props); },
  };
}
