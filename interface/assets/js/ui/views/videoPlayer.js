import { create } from './panelDom.js';

const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;

/** Avisa a página: com vídeo tocando, a voz da Kira fica baixa (index.html escuta 'kira:video-playing'). */
function announce(windowRef, playing) {
  try { windowRef?.dispatchEvent?.(new CustomEvent('kira:video-playing', { detail: { playing } })); } catch (_) { /* sem janela */ }
}

/**
 * Player sob demanda: nada carrega até "Reproduzir"; só youtube-nocookie com sandbox; destroy() remove o iframe.
 * Só começa depois do clique do usuário em "Reproduzir".
 */
export function createVideoPlayer(documentRef, host, { windowRef = documentRef.defaultView || globalThis } = {}) {
  let frame = null;
  const destroy = () => {
    if (!frame) return;
    frame.remove?.();
    frame.parentNode?.removeChild?.(frame);
    frame = null;
    announce(windowRef, false);
  };
  const play = (videoId) => {
    if (!VIDEO_ID_RE.test(String(videoId || ''))) return false;
    destroy();
    frame = create(documentRef, 'iframe', 'kui-video-frame');
    frame.src = `https://www.youtube-nocookie.com/embed/${videoId}?autoplay=1`;
    frame.title = 'Vídeo incorporado';
    frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation');
    frame.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');
    frame.setAttribute('allow', 'autoplay; encrypted-media; picture-in-picture');
    frame.allowFullscreen = true;
    host.appendChild(frame);
    announce(windowRef, true);
    return true;
  };
  return { play, destroy, get playing() { return Boolean(frame); } };
}

export { VIDEO_ID_RE };
