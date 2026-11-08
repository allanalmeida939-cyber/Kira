/* Telinha ao vivo (2026-10-31): com a webcam ou a tela ligadas, um vídeo pequeno no canto de cima à direita mostra o que
   a Kira está vendo. Usa a própria trilha que o navegador já manda ao LiveKit (não abre a câmera de novo, não custa
   nada a mais). Uma telinha por fonte, empilhadas; o X esconde só a telinha (a Kira continua vendo). No celular, nada. */
(function () {
  if (window.KIRA_REMOTE) return;
  const box = document.createElement('div');
  box.className = 'kira-live-previews';
  box.setAttribute('aria-label', 'O que a Kira está vendo');
  document.body.appendChild(box);
  const shown = new Map(); // fonte -> { el, track }
  const dismissed = new Set();
  const LABEL = { camera: 'Webcam', screen_share: 'Tela' };

  function tracks() {
    const out = new Map();
    const participant = window.room?.localParticipant || (typeof room !== 'undefined' ? room?.localParticipant : null);
    const pubs = participant?.trackPublications || participant?.videoTrackPublications;
    for (const pub of pubs?.values?.() || []) {
      const source = String(pub.source || pub.track?.source || '');
      const media = pub.track?.mediaStreamTrack;
      if (LABEL[source] && media && media.readyState === 'live' && !pub.isMuted) out.set(source, media);
    }
    return out;
  }

  function add(source, media) {
    const el = document.createElement('figure');
    el.className = 'kira-live-preview';
    el.innerHTML = `<figcaption><i></i>${LABEL[source]} · a Kira vê isto</figcaption><button type="button" aria-label="Esconder a telinha">×</button>`;
    const video = document.createElement('video');
    video.muted = true; video.autoplay = true; video.playsInline = true;
    video.srcObject = new MediaStream([media]);
    el.prepend(video);
    el.querySelector('button').addEventListener('click', () => { dismissed.add(source); remove(source); });
    box.appendChild(el);
    shown.set(source, { el, media });
  }

  function remove(source) {
    const item = shown.get(source);
    if (!item) return;
    item.el.remove();
    shown.delete(source);
  }

  function sync() {
    const live = tracks();
    for (const source of [...shown.keys()]) {
      if (!live.has(source) || live.get(source) !== shown.get(source).media) remove(source);
    }
    for (const [source, media] of live) {
      if (!shown.has(source) && !dismissed.has(source)) add(source, media);
    }
    for (const source of [...dismissed]) if (!live.has(source)) dismissed.delete(source); // desligou: na próxima vez volta a aparecer
    document.body.classList.toggle('kira-live-preview-on', shown.size > 0);
  }

  setInterval(sync, 1000);
})();
