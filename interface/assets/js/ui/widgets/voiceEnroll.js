// Barra do cadastro de voz: enche enquanto a pessoa fala ("Gravando a voz do Chefe… continue falando"); cheia, avisa
// para parar e mostra se a voz foi salva. O agente manda o progresso (runtime/voice_enrollment.py); só textContent.

const HIDE_AFTER_MS = { salva: 4000, falhou: 9000, cancelado: 2500 };
// Jeitos diferentes de falar ao longo da barra (como o Google e a Alexa pedem várias frases): a voz do dia a dia muda.
export const GUIDE = [
  [0.2, 'Fale normalmente: conte como foi seu dia.'],
  [0.4, 'Agora comandos: "Kira, abre o YouTube", "Kira, manda mensagem pro Breno".'],
  [0.6, 'Agora um pouco mais alto, como se estivesse longe do microfone.'],
  [0.8, 'Agora mais baixo, quase cochichando.'],
  [1.01, 'Para fechar: fale rápido, do jeito do dia a dia.'],
];

function el(doc, tag, className = '', text = '') {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function createVoiceEnroll({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  const doc = documentRef;
  const win = windowRef;
  let hideTimer = 0;

  const box = el(doc, 'div', 'kira-voice-enroll');
  box.hidden = true;
  box.setAttribute('role', 'status');
  box.setAttribute('aria-live', 'polite');
  const title = el(doc, 'div', 'kira-voice-enroll-title');
  const track = el(doc, 'div', 'kira-voice-enroll-track');
  const fill = el(doc, 'div', 'kira-voice-enroll-fill');
  track.appendChild(fill);
  const hint = el(doc, 'div', 'kira-voice-enroll-hint');
  box.append(title, track, hint);
  doc.body.appendChild(box);

  const api = {
    update({ state = '', name = '', progress = 0, message = '' } = {}) {
      const who = String(name || '').slice(0, 30);
      const percent = Math.round(Math.max(0, Math.min(1, Number(progress) || 0)) * 100);
      win.clearTimeout(hideTimer);
      box.hidden = false;
      box.dataset.state = state;
      fill.style.width = `${percent}%`;
      if (state === 'gravando') {
        title.textContent = `Gravando a voz ${who ? `de ${who}` : ''} · ${percent}%`;
        hint.textContent = GUIDE.find(([until]) => percent / 100 < until)[1];
      } else if (state === 'salvando') {
        title.textContent = 'Pronto, pode parar de falar';
        hint.textContent = 'Salvando a voz…';
      } else if (state === 'salva') {
        title.textContent = `Voz ${who ? `de ${who} ` : ''}salva ✓`;
        hint.textContent = '';
      } else if (state === 'falhou') {
        title.textContent = 'Voz não salva';
        hint.textContent = String(message || '').slice(0, 200);
      } else {
        title.textContent = 'Cadastro de voz cancelado';
        hint.textContent = '';
      }
      if (HIDE_AFTER_MS[state]) hideTimer = win.setTimeout(() => { box.hidden = true; }, HIDE_AFTER_MS[state]);
      return true;
    },
  };
  return api;
}

if (globalThis.document?.body && globalThis.window && !globalThis.window.KiraVoiceEnroll) {
  globalThis.window.KiraVoiceEnroll = createVoiceEnroll();
}
