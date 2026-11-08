// Voz no celular: a página aberta pelo portão (window.KIRA_REMOTE) entra na mesma sala de voz do PC como o participante
// "Celular" (services/phone_api.py). Ao entrar, só ouve; o botão de microfone abre a escuta e a Kira passa a ouvir o
// celular (atributo "kira.talk", agent/agent.py). A voz da Kira toca aqui só com o celular como aparelho ativo; usar o PC
// devolve a voz para lá e fecha o microfone daqui. Microfone exige o link seguro (HTTPS); ouvir funciona em qualquer link.

const STATE_EVERY_MS = 3000;
const VOICE_BEAT_MS = 30000;
const MIC_TAIL_MS = 500; // cauda ao soltar o microfone: o fim da frase não é cortado

function isAgent(participant) {
  const kind = globalThis.LivekitClient?.ParticipantKind?.AGENT;
  return Boolean(participant?.isAgent || (kind !== undefined && participant?.kind === kind) || /^agent[-_]/i.test(String(participant?.identity || '')));
}

export function createRemoteVoice({ documentRef = globalThis.document, windowRef = globalThis.window, fetchRef = globalThis.fetch } = {}) {
  const doc = documentRef;
  const win = windowRef;
  let room = null;
  let joining = null;
  let micOn = false;
  let active = true;
  let beat = null;
  const audioEls = new Set();

  const button = doc.createElement('button');
  button.type = 'button';
  button.id = 'remote-mic-btn';
  button.className = 'remote-mic';
  button.setAttribute('aria-label', 'Falar com a Kira');
  const label = doc.createElement('span');
  label.className = 'remote-mic-label';
  button.append(label);
  doc.body.append(button);

  const note = (text) => {
    label.textContent = text;
    win.setComposerStatus?.(text === 'Falar' ? '' : text);
  };

  const post = (path, body) => fetchRef(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}), cache: 'no-store',
  }).then((response) => response.json()).catch(() => ({ ok: false }));

  function render() {
    button.classList.toggle('is-on', micOn);
    button.classList.toggle('is-joining', Boolean(joining));
    button.setAttribute('aria-pressed', micOn ? 'true' : 'false');
    if (joining) note('Conectando…');
    else note(micOn ? 'Ouvindo…' : 'Falar');
  }

  function applyOutput() {
    // Celular ativo: a voz toca aqui. PC ativo: aqui fica mudo (só a transcrição).
    audioEls.forEach((el) => { el.muted = !active; });
  }

  function attach(track, participant) {
    const LK = win.LivekitClient;
    if (!LK || track.kind !== LK.Track.Kind.Audio || !isAgent(participant)) return;
    const el = track.attach();
    el.autoplay = true;
    el.dataset.kiraPhoneVoice = '1';
    el.style.display = 'none';
    doc.body.append(el);
    audioEls.add(el);
    win.AkiraOrbVoiceAnimator?.connectAudioSource?.(el, 'phone_voice'); // o orbe reage à fala da Kira aqui também
    applyOutput();
  }

  function subscribeAgent(publication, participant) {
    const LK = win.LivekitClient;
    if (LK && publication.kind === LK.Track.Kind.Audio && isAgent(participant)) publication.setSubscribed(true);
  }

  async function join() {
    if (room) return room;
    if (joining) return joining;
    const LK = win.LivekitClient;
    if (!LK?.Room) {
      note('Voz indisponível neste navegador.');
      return null;
    }
    joining = (async () => {
      render();
      const access = await post('/api/phone/voice/join');
      if (!access?.ok) {
        note(access?.error || 'A voz pelo celular não abriu agora.');
        return null;
      }
      const next = new LK.Room({ adaptiveStream: true, dynacast: true });
      next.on(LK.RoomEvent.TrackPublished, subscribeAgent);
      next.on(LK.RoomEvent.TrackSubscribed, (track, _publication, participant) => attach(track, participant));
      // a fala da Kira, trecho a trecho: o painel de imagens troca de foto conforme ela conta (no PC isto vem do
      // index.html; no celular faltava e a história ficava presa na 1ª imagem)
      next.on(LK.RoomEvent.TranscriptionReceived, (segments, participant) => {
        if (!isAgent(participant)) return;
        (segments || []).forEach((seg) => seg?.text && win.dispatchEvent(new win.CustomEvent('kira:spoken', { detail: { id: String(seg.id || ''), text: String(seg.text), final: Boolean(seg.final) } })));
      });
      next.on(LK.RoomEvent.TrackUnsubscribed, (track) => track.detach().forEach((el) => { audioEls.delete(el); el.remove(); }));
      // o próprio celular parou a tela (aviso do sistema): a Kira deixa de ver
      next.on(LK.RoomEvent.LocalTrackUnpublished, (publication) => {
        const source = publication?.source;
        if ((seeing === 'screen' && source === LK.Track.Source.ScreenShare) || (seeing === 'camera' && source === LK.Track.Source.Camera)) {
          seeing = null;
          showPreview();
          post('/api/phone/vision', { on: false });
        }
      });
      next.on(LK.RoomEvent.Disconnected, () => {
        room = null;
        micOn = false;
        seeing = null;
        showPreview();
        render();
      });
      await next.connect(access.url, access.token, { autoSubscribe: false });
      next.remoteParticipants.forEach((participant) => participant.trackPublications.forEach((publication) => subscribeAgent(publication, participant)));
      room = next;
      try { await next.startAudio(); } catch (_) { /* sem toque ainda: o próximo toque libera o som */ }
      return next;
    })().catch(() => {
      note('A voz pelo celular não abriu agora.');
      return null;
    }).finally(() => {
      joining = null;
      render();
    });
    return joining;
  }

  async function setMic(on) {
    if (on && !win.isSecureContext) {
      note('Microfone só pelo link seguro (https). Escreva ou abra pelo link fixo.');
      return;
    }
    const current = await join();
    if (!current) return;
    try { await current.startAudio(); } catch (_) { /* segue */ }
    // Soltar o botão no fim da palavra cortava a última sílaba: o microfone fica aberto mais meio segundo (2026-11-06).
    if (!on) await new Promise((resolve) => (typeof win.setTimeout === "function" ? win.setTimeout(resolve, MIC_TAIL_MS) : setTimeout(resolve, MIC_TAIL_MS)));
    try {
      await current.localParticipant.setMicrophoneEnabled(on);
      await current.localParticipant.setAttributes({ 'kira.talk': on ? '1' : '0' });
    } catch (_) {
      note('Não consegui abrir o microfone. Confira a permissão do navegador.');
      return;
    }
    micOn = on;
    if (on) {
      active = true;
      applyOutput();
    }
    win.clearInterval(beat);
    beat = on ? win.setInterval(() => post('/api/phone/voice', { on: true }), VOICE_BEAT_MS) : null;
    post('/api/phone/voice', { on });
    render();
  }

  // Câmera e tela do celular: publicadas na sala de voz, a Kira passa a ver (o agente pega o vídeo de quem publicar).
  // Uma prévia pequena no canto mostra o que ela está vendo, com trocar câmera e parar.
  let seeing = null; // 'camera' | 'screen' | null
  let facing = 'environment';
  const preview = doc.createElement('div');
  preview.className = 'remote-eye';
  preview.hidden = true;
  const video = doc.createElement('video');
  video.muted = true;
  video.playsInline = true;
  video.autoplay = true;
  video.setAttribute('playsinline', '');
  const eyeLabel = doc.createElement('span');
  eyeLabel.className = 'remote-eye-label';
  const flip = doc.createElement('button');
  flip.type = 'button';
  flip.className = 'remote-eye-flip';
  flip.setAttribute('aria-label', 'Trocar câmera');
  flip.addEventListener('click', () => flipCamera());
  const stop = doc.createElement('button');
  stop.type = 'button';
  stop.className = 'remote-eye-stop';
  stop.setAttribute('aria-label', 'Parar: a Kira deixa de ver');
  stop.textContent = '✕';
  stop.addEventListener('click', () => setVision(null));
  preview.append(video, eyeLabel, flip, stop);
  doc.body.append(preview);

  function showPreview() {
    const LK = win.LivekitClient;
    const source = seeing === 'screen' ? LK?.Track?.Source?.ScreenShare : LK?.Track?.Source?.Camera;
    const track = seeing && room?.localParticipant?.getTrackPublication?.(source)?.track;
    preview.hidden = !track;
    preview.classList.toggle('is-screen', seeing === 'screen');
    eyeLabel.textContent = seeing === 'screen' ? 'Kira vendo a tela' : 'Kira vendo';
    if (track) track.attach(video);
  }

  async function setVision(next) {
    const want = next === 'camera' || next === 'screen' ? next : null;
    if (want && !win.isSecureContext) {
      note('Câmera e tela só pelo link seguro (https).');
      return { ok: false };
    }
    if (want === 'screen' && !win.navigator?.mediaDevices?.getDisplayMedia) {
      note('Este celular não deixa o navegador compartilhar a tela. Use a câmera, ou o PC para mostrar a tela.');
      return { ok: false, error: 'screen_unsupported' };
    }
    const current = await join();
    if (!current) return { ok: false };
    const local = current.localParticipant;
    try {
      if (seeing === 'camera' && want !== 'camera') await local.setCameraEnabled(false);
      if (seeing === 'screen' && want !== 'screen') await local.setScreenShareEnabled(false);
      if (want === 'camera') await local.setCameraEnabled(true, { facingMode: facing, resolution: { width: 1280, height: 720 } });
      if (want === 'screen') await local.setScreenShareEnabled(true, { audio: false });
    } catch (_) {
      note(want === 'screen' ? 'Não consegui compartilhar a tela daqui.' : 'Não consegui abrir a câmera. Confira a permissão do navegador.');
      seeing = null;
      showPreview();
      post('/api/phone/vision', { on: false });
      return { ok: false };
    }
    seeing = want;
    showPreview();
    await post('/api/phone/vision', { on: Boolean(want), source: want || 'camera' });
    note(want ? (want === 'screen' ? 'A Kira está vendo a tela do celular.' : 'A Kira está vendo pela câmera.') : 'Falar');
    win.dispatchEvent?.(new win.CustomEvent('kira:phone-vision', { detail: { seeing } }));
    return { ok: true, seeing };
  }

  async function flipCamera() {
    if (seeing !== 'camera' || !room) return;
    facing = facing === 'environment' ? 'user' : 'environment';
    const LK = win.LivekitClient;
    const track = room.localParticipant.getTrackPublication?.(LK?.Track?.Source?.Camera)?.track;
    try { await track?.restartTrack?.({ facingMode: facing }); } catch (_) { note('Não consegui trocar a câmera.'); }
    showPreview();
  }

  async function poll() {
    try {
      const response = await fetchRef('/api/phone/state', { cache: 'no-store' });
      const state = await response.json();
      active = state?.device === 'celular';
      applyOutput();
      if (!active && micOn) setMic(false); // o Chefe voltou para o PC
      if (!active && seeing) setVision(null);
    } catch (_) { /* sem rede por um instante: tenta de novo */ }
  }

  button.addEventListener('click', () => setMic(!micOn));
  win.setInterval(poll, STATE_EVERY_MS);
  render();
  return { join, setMic, setVision, flipCamera, poll, get micOn() { return micOn; }, get active() { return active; }, get seeing() { return seeing; } };
}

if (globalThis.window?.KIRA_REMOTE && globalThis.document?.body && !globalThis.window.KiraRemoteVoice) {
  globalThis.window.KiraRemoteVoice = createRemoteVoice();
}
