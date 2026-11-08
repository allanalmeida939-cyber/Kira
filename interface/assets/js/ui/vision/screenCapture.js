// Captura para a visão da Kira. O seletor do app (services/screen_share_picker.py) pergunta o que mostrar:
// um monitor ou uma janela vira o vídeo direto; "todos os monitores" pede uma captura por monitor (as extras
// são respondidas sem perguntar) e junta tudo lado a lado num canvas, publicado como um único vídeo no LiveKit.

const FPS = 5;

function stopStream(stream) {
  stream?.getTracks?.().forEach((track) => track.stop());
}

/** Monitores lado a lado, alinhados no topo, cada um no tamanho real. */
export function layoutSideBySide(sizes) {
  let x = 0;
  const rects = sizes.map((size) => {
    const rect = { x, y: 0, width: size.width, height: size.height };
    x += size.width;
    return rect;
  });
  return { width: Math.max(1, x), height: Math.max(1, ...sizes.map((size) => size.height)), rects };
}

async function playing(stream, documentRef) {
  const video = documentRef.createElement('video');
  video.muted = true;
  video.playsInline = true;
  video.srcObject = stream;
  await video.play();
  if (!video.videoWidth) await new Promise((resolve) => video.addEventListener('loadedmetadata', resolve, { once: true }));
  return video;
}

async function composite(streams, { documentRef, windowRef, fps, choice }) {
  const videos = await Promise.all(streams.map((stream) => playing(stream, documentRef)));
  const layout = layoutSideBySide(videos.map((video) => ({ width: video.videoWidth, height: video.videoHeight })));
  const canvas = documentRef.createElement('canvas');
  canvas.width = layout.width;
  canvas.height = layout.height;
  const context = canvas.getContext('2d', { alpha: false });
  const draw = () => {
    context.fillStyle = '#000';
    context.fillRect(0, 0, canvas.width, canvas.height);
    videos.forEach((video, index) => {
      const rect = layout.rects[index];
      context.drawImage(video, rect.x, rect.y, rect.width, rect.height);
    });
  };
  draw();
  const timer = windowRef.setInterval(draw, Math.round(1000 / fps));
  const track = canvas.captureStream(fps).getVideoTracks()[0];
  let stopped = false;
  const stop = () => {
    if (stopped) return;
    stopped = true;
    windowRef.clearInterval(timer);
    track.stop();
    streams.forEach(stopStream);
    videos.forEach((video) => { video.srcObject = null; });
  };
  // Uma captura encerrada pelo Windows (monitor desligado) encerra o vídeo junto.
  streams.forEach((stream) => stream.getVideoTracks()[0]?.addEventListener?.('ended', stop, { once: true }));
  return { track, choice, stop, layout };
}

export async function captureForKira({
  mediaDevices = globalThis.navigator?.mediaDevices,
  fetchChoice = null,
  documentRef = globalThis.document,
  windowRef = globalThis.window,
  fps = FPS,
} = {}) {
  const constraints = { video: { frameRate: fps }, audio: false };
  const first = await mediaDevices.getDisplayMedia(constraints);
  let choice = {};
  try {
    choice = (await fetchChoice?.()) || {};
  } catch (_) {
    choice = {};
  }
  const count = choice.kind === 'all' ? Math.max(1, Math.min(8, Number(choice.screens) || 1)) : 1;
  const streams = [first];
  try {
    for (let index = 1; index < count; index += 1) streams.push(await mediaDevices.getDisplayMedia(constraints));
  } catch (error) {
    streams.forEach(stopStream);
    throw error;
  }
  if (streams.length === 1) return { track: first.getVideoTracks()[0], choice, stop: () => stopStream(first) };
  return composite(streams, { documentRef, windowRef, fps, choice });
}

globalThis.KiraScreenCapture = Object.freeze({ captureForKira });
