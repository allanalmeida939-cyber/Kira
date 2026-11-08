import { clear, create, safeHttps, safeImage, safeText } from './panelDom.js';
import { VIDEO_ID_RE, createVideoPlayer } from './videoPlayer.js';
import { openExternal } from './presentation/components.js';

function videoOptions(props = {}) {
  const items = Array.isArray(props.items) ? props.items : [];
  const raw = items.length ? items : [props];
  return raw
    .map((item) => ({ videoId: safeText(item?.videoId), title: safeText(item?.title, 'Vídeo'), thumbnail: safeImage(item?.thumbnail), url: safeHttps(item?.url || item?.sourceUrl), embeddable: item?.embeddable !== false }))
    .filter((item) => VIDEO_ID_RE.test(item.videoId))
    .slice(0, 6);
}

/** Opções de vídeo; o player só carrega quando o usuário clica em "Reproduzir" e some ao fechar, ocultar ou trocar. */
export function createVideoPanel({ documentRef = globalThis.document, open = openExternal } = {}) {
  return {
    mount(container, context) {
      const documentForPanel = container.ownerDocument || documentRef;
      const root = create(documentForPanel, 'div', 'kui-video-panel');
      const stage = create(documentForPanel, 'div', 'kui-video-stage');
      const list = create(documentForPanel, 'div', 'kui-video-alternatives');
      root.appendChild(stage);
      root.appendChild(list);
      container.appendChild(root);
      const player = createVideoPlayer(documentForPanel, stage);
      const render = (props = {}) => {
        player.destroy();
        clear(list);
        const options = videoOptions(props);
        options.forEach((item) => {
          const row = create(documentForPanel, 'div', 'kui-video-option');
          if (item.thumbnail) {
            const thumb = create(documentForPanel, 'img', 'kui-video-thumb');
            thumb.src = item.thumbnail; thumb.alt = ''; thumb.loading = 'lazy';
            row.appendChild(thumb);
          }
          row.appendChild(create(documentForPanel, 'span', 'kui-video-title', item.title));
          const playButton = create(documentForPanel, 'button', 'kui-panel-button', item.embeddable ? 'Reproduzir' : 'Não incorporável');
          playButton.type = 'button';
          playButton.disabled = !item.embeddable;
          playButton.addEventListener?.('click', () => player.play(item.videoId));
          const openButton = create(documentForPanel, 'button', 'kui-panel-button', 'Abrir no navegador');
          openButton.type = 'button';
          openButton.disabled = !item.url;
          openButton.addEventListener?.('click', () => { player.destroy(); open(item.url); });
          row.appendChild(playButton);
          row.appendChild(openButton);
          list.appendChild(row);
        });
        if (!options.length) list.appendChild(create(documentForPanel, 'p', 'kui-panel-empty', 'Vídeo indisponível.'));
        if (props.state === 'play' && options[0]?.embeddable) player.play(options[0].videoId); // clicado na vitrine: já toca
      };
      render(context.props);
      container.__kuiVideoPanel = { update: render, hide: () => player.destroy() };
      return () => { player.destroy(); delete container.__kuiVideoPanel; };
    },
    update(container, props) { container.__kuiVideoPanel?.update?.(props); },
    visibilityChange(container, visible) { if (!visible) container.__kuiVideoPanel?.hide?.(); },
  };
}
