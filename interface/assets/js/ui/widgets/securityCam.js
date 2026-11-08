// Câmera de segurança: voz desconhecida insistindo em comandos -> a Kira liga a webcam, mostra ao vivo, grava até
// 60 s e tira uma foto; tudo vai para Vídeos\Kira Segurança (o backend salva).

const RECORD_MS = 60000;
const PHOTO_AFTER_MS = 1500;

function el(doc, tag, className = '', text = '') {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function createSecurityCam({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  const doc = documentRef;
  const win = windowRef;
  let stream = null;
  let recorder = null;
  let chunks = [];
  let timers = [];

  const box = el(doc, 'div', 'kira-security-cam');
  box.hidden = true;
  box.setAttribute('role', 'alert');
  const head = el(doc, 'div', 'kira-security-cam-head');
  const status = el(doc, 'span', 'kira-security-cam-status', 'GRAVANDO');
  head.append(el(doc, 'span', 'kira-security-cam-dot'), el(doc, 'span', '', 'CÂMERA DE SEGURANÇA'), status);
  const video = el(doc, 'video');
  video.muted = true;
  video.autoplay = true;
  video.playsInline = true;
  box.append(head, video);
  doc.body.appendChild(box);

  const upload = async (blob, kind) => {
    try {
      const response = await win.fetch(`/api/security/recording?kind=${kind}`, { method: 'POST', body: blob });
      return response.ok;
    } catch (_) {
      return false;
    }
  };

  const photo = () => {
    const canvas = doc.createElement('canvas');
    canvas.width = video.videoWidth || 640;
    canvas.height = video.videoHeight || 480;
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((blob) => blob && upload(blob, 'photo'), 'image/jpeg', 0.9);
  };

  const api = {
    isRecording: () => Boolean(recorder),
    async start() {
      if (recorder) return { ok: true, already: true };
      try {
        stream = await win.navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 }, audio: false });
      } catch (error) {
        return { ok: false, error: String(error?.name || error) };
      }
      video.srcObject = stream;
      box.hidden = false;
      status.textContent = 'GRAVANDO';
      chunks = [];
      recorder = new win.MediaRecorder(stream, { mimeType: 'video/webm;codecs=vp8' });
      recorder.ondataavailable = (event) => event.data?.size && chunks.push(event.data);
      recorder.onstop = async () => {
        const saved = chunks.length ? await upload(new win.Blob(chunks, { type: 'video/webm' }), 'video') : false;
        status.textContent = saved ? 'SALVO' : 'FALHOU AO SALVAR';
        stream?.getTracks().forEach((track) => track.stop());
        stream = null;
        recorder = null;
        timers.push(win.setTimeout(() => { box.hidden = true; }, 4000));
      };
      recorder.start(1000);
      timers = [win.setTimeout(photo, PHOTO_AFTER_MS), win.setTimeout(() => api.stop(), RECORD_MS)];
      return { ok: true };
    },
    stop() {
      timers.forEach((id) => win.clearTimeout(id));
      timers = [];
      if (recorder && recorder.state !== 'inactive') recorder.stop();
      return { ok: true };
    },
  };
  return api;
}

if (globalThis.document?.body && globalThis.window && !globalThis.window.KiraSecurityCam) {
  globalThis.window.KiraSecurityCam = createSecurityCam();
}
