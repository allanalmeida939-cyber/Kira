// Tela de bloqueio da Kira (Fase 5). Cobre a janela principal; só sai com a senha (conferida no servidor).
// Segurar Espaço liga o microfone (falar com a Kira), soltar desliga; voz nunca desbloqueia.
// Aviso honesto: isto não tranca o Windows. Para isso existe "Kira, bloquear o PC" (bloqueio nativo).
import './appearance/thinkingOrbs.js';

const doc = document;
let box = null;
let clockTimer = 0;
let micBefore = false;
let holding = false;
let beatTimer = 0; // sinal de vida do bloqueio forte: sem ele o teclado/mouse se soltam em 90 s (kira_lock_guard.py)

const post = (action, body = {}) => fetch(`/api/lock/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  .then((r) => r.json()).catch(() => ({ ok: false, error: 'Kira sem resposta' }));
const h = (tag, cls, text) => { const n = doc.createElement(tag); if (cls) n.className = cls; if (text) n.textContent = text; return n; };

function build() {
  box = h('div', 'kira-lock');
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  box.setAttribute('aria-label', 'Kira bloqueada');
  const orb = h('kira-orb');
  orb.setAttribute('state', 'trancando');
  orb.setAttribute('size', '200');
  const rings = h('div', 'kira-lock-rings');
  rings.append(h('i'), h('i'), h('i'), orb);
  const clock = h('div', 'kira-lock-clock');
  const date = h('div', 'kira-lock-date');
  const form = h('form', 'kira-lock-form');
  const input = h('input', 'kira-lock-pass');
  Object.assign(input, { type: 'password', placeholder: 'Senha', autocomplete: 'current-password' });
  input.setAttribute('aria-label', 'Senha da Kira');
  const go = h('button', 'kira-lock-go', '→');
  go.type = 'submit';
  go.setAttribute('aria-label', 'Desbloquear');
  form.append(input, go);
  const msg = h('div', 'kira-lock-msg');
  msg.setAttribute('aria-live', 'polite');
  const mic = h('div', 'kira-lock-mic', 'Segure Espaço para ativar o mic');
  const talk = h('button', 'kira-lock-talk', 'Segurar para falar'); // opção acessível (toque/mouse)
  talk.type = 'button';
  const face = h('button', 'kira-lock-face', 'Rosto: indisponível');
  face.type = 'button';
  face.disabled = true;
  face.title = 'Webcam comum não tem prova de vida confiável; use a senha.';
  box.append(rings, clock, date, form, msg, mic, h('div', 'kira-lock-row'));
  box.lastChild.append(talk, face);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (box.__setup) { // criar senha: digita duas vezes, depois tranca
      if (!box.__first) { box.__first = input.value; input.value = ''; msg.textContent = 'Digite de novo para confirmar'; return; }
      const same = box.__first === input.value;
      const first = box.__first;
      box.__first = '';
      input.value = '';
      if (!same) { msg.textContent = 'Não bateu. Digite a nova senha'; return; }
      const made = await post('password', { new: first });
      if (!made.ok) { msg.textContent = made.error || 'Não deu'; return; }
      box.__setup = false;
      input.placeholder = 'Senha';
      msg.textContent = '';
      await post('lock');
      return;
    }
    msg.textContent = 'Conferindo…';
    const r = await post('unlock', { password: input.value });
    input.value = '';
    if (r.ok) unlockAnimation();
    else { msg.textContent = r.error || 'Senha errada.'; box.classList.remove('is-shake'); void box.offsetWidth; box.classList.add('is-shake'); }
  });
  talk.addEventListener('pointerdown', () => hold(true));
  ['pointerup', 'pointerleave', 'pointercancel'].forEach((ev) => talk.addEventListener(ev, () => hold(false)));
  const tick = () => {
    const now = new Date();
    clock.textContent = now.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
    date.textContent = now.toLocaleDateString('pt-BR', { weekday: 'long', day: 'numeric', month: 'long' });
  };
  tick();
  box.__tick = tick;
  box.__input = input;
  box.__mic = mic;
  doc.body.append(box);
}

function hold(on) {
  if (!box || box.hidden || holding === on) return;
  holding = on;
  box.__mic.classList.toggle('is-on', on);
  box.__mic.textContent = on ? 'Ouvindo… solte para desligar' : 'Segure Espaço para ativar o mic';
  window.kiraSetMic?.(on);
}

function onKey(e) {
  if (!box || box.hidden) return;
  if (e.code === 'Space' && e.target !== box.__input) { e.preventDefault(); e.stopPropagation(); if (!e.repeat) hold(e.type === 'keydown'); return; }
  // nada passa para a Kira por baixo (atalhos de painel, Olho de Deus...), só o campo de senha
  if (e.target !== box.__input) { e.stopPropagation(); if (e.key !== 'Tab') e.preventDefault(); }
}

function show(keepMic = false) {
  if (!box) build();
  if (!box.hidden && box.isConnected && box.classList.contains('is-on')) return;
  micBefore = Boolean(window.kiraMicOn?.());
  if (!keepMic) window.kiraSetMic?.(false);
  box.hidden = false;
  box.classList.remove('is-leaving');
  box.classList.add('is-on');
  doc.documentElement.classList.add('kira-locked');
  clearInterval(clockTimer);
  clockTimer = setInterval(box.__tick, 1000);
  clearInterval(beatTimer);
  beatTimer = setInterval(() => fetch('/api/lock/state').catch(() => {}), 5000);
  setTimeout(() => box.__input.focus(), 50);
}

function unlockAnimation() {
  hold(false);
  const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
  box.querySelector('kira-orb').setAttribute('state', 'conectando');
  box.classList.add('is-leaving');
  doc.documentElement.classList.remove('kira-locked'); // os painéis voltam juntos com a animação
  setTimeout(() => {
    box.hidden = true;
    box.classList.remove('is-on', 'is-leaving');
    box.querySelector('kira-orb').setAttribute('state', 'trancando');
    clearInterval(clockTimer);
    clearInterval(beatTimer);
    if (micBefore) window.kiraSetMic?.(true);
  }, still ? 50 : 1400);
}

addEventListener('keydown', onKey, true);
addEventListener('keyup', onKey, true);
function setup() {
  show(true);
  box.__setup = true;
  box.__first = '';
  box.__input.placeholder = 'Crie a senha da Kira';
  box.querySelector('.kira-lock-msg').textContent = 'Crie uma senha só com letras (mín. 4). Esc cancela.';
}
addEventListener('keydown', (e) => { if (e.key === 'Escape' && box?.__setup && !box.hidden) { box.__setup = false; unlockAnimation(); } }, true);
window.KiraLock = { show, setup, lock: () => post('lock') };
// reinício com a Kira trancada: volta trancada
fetch('/api/lock/state').then((r) => r.json()).then((s) => { if (s.locked) show(); }).catch(() => {});
