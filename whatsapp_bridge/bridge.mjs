// Bridge do WhatsApp da Kira: Baileys (protocolo do WhatsApp Web, sem navegador) + HTTP local.
// Contrato igual ao bridge antigo (porta 5050: /status, /send, /messages/new, /messages/new/agent, /log, /shutdown;
// porta 5100: /start_bridge) e mais /send-media, /messages/recent, /logout e, para a secretária,
// /messages/new/secretary e /reply. Tudo exceto /status pede o token interno da Kira no cabeçalho X-Kira-Internal.
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import makeWASocket, { Browsers, DisconnectReason, downloadMediaMessage, fetchLatestBaileysVersion, isJidGroup, useMultiFileAuthState } from '@whiskeysockets/baileys';
import pino from 'pino';
import QRCode from 'qrcode';

import { Inbox, isOwnChat, kiraContactFor, mediaKind, messageText, normalizePhone, phoneFromJid, phoneJid, resolveContact } from './lib.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DATA_DIR = path.join(HERE, 'data');
const AUTH_DIR = path.join(DATA_DIR, 'auth');
const MEDIA_IN_DIR = path.join(DATA_DIR, 'media_in'); // fotos/figurinhas/vídeos recebidos para a Kira ver
const CONTACTS_FILE = path.join(DATA_DIR, 'contacts.json');
const KIRA_CONTACTS_FILE = process.env.KIRA_WHATSAPP_CONTACTS || path.join(HERE, '..', 'configs', 'whatsapp_contacts.json');
const TOKEN = process.env.KIRA_INTERNAL_TOKEN || '';
const PORT = Number(process.env.KIRA_WHATSAPP_PORT || 5050);
const ADMIN_PORT = Number(process.env.KIRA_WHATSAPP_ADMIN_PORT || 5100);
const MAX_MEDIA_BYTES = 64 * 1024 * 1024; // limite do WhatsApp para vídeo/arquivo comum
const AUDIO_MIME = { mp3: 'audio/mpeg', ogg: 'audio/ogg; codecs=opus', opus: 'audio/ogg; codecs=opus', m4a: 'audio/mp4', aac: 'audio/aac', wav: 'audio/wav' };
const REPLY_WINDOW_MS = 30 * 60 * 1000; // a secretária só responde no chat de quem escreveu nos últimos 30 min

const state = { status: 'starting', qr: null, me: null, lastError: null };
const logLines = [];
const uiInbox = new Inbox();
const agentInbox = new Inbox();
const recent = new Inbox(300);
// Secretária: só mensagens de quem está na lista da Kira (as outras nem chegam a ela) e o aviso de que o Chefe
// respondeu pelo celular (aí ela sai da conversa).
const secretaryInbox = new Inbox();
const ownSent = new Set(); // ids do que esta ponte mandou; o resto "fromMe" foi o Chefe, pelo celular
const ownSentText = new Set(); // textos que a ponte acabou de mandar (o eco no chat próprio pode chegar antes do id)
const synced = new Map(loadJson(CONTACTS_FILE, []));
let sock = null;
let stopping = false;
let attempts = 0;
let saveTimer = null;

function log(line) {
  const stamped = `${new Date().toISOString().slice(11, 19)} ${line}`;
  logLines.push(stamped);
  if (logLines.length > 200) logLines.shift();
  console.log(`[bridge] ${line}`);
}

function loadJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return fallback;
  }
}

function kiraContacts() {
  const data = loadJson(KIRA_CONTACTS_FILE, {});
  return Array.isArray(data?.contacts) ? data.contacts : [];
}

function rememberContacts(list) {
  for (const contact of list || []) {
    const id = contact.phoneNumber || contact.id;
    if (!id || isJidGroup(id)) continue;
    const old = synced.get(id) || {};
    synced.set(id, { name: contact.name || old.name || '', notify: contact.notify || contact.verifiedName || old.notify || '', last: old.last || 0 });
  }
  scheduleSave();
}

/** Última conversa com cada contato (segundos): desempata dois contatos com o mesmo nome na agenda. */
function noteActivity(jid, seconds) {
  if (!jid?.endsWith('@s.whatsapp.net') || !seconds) return;
  const info = synced.get(jid) || { name: '', notify: '' };
  if (seconds <= (info.last || 0)) return;
  synced.set(jid, { ...info, last: seconds });
  scheduleSave();
}

function onHistory({ chats, contacts }) {
  rememberContacts(contacts);
  for (const chat of chats || []) {
    const jid = chat.pnJid || chat.id;
    const ts = chat.conversationTimestamp;
    noteActivity(jid, Number(ts?.toNumber ? ts.toNumber() : ts || 0)); // pode vir como Long
  }
}

function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(CONTACTS_FILE, JSON.stringify([...synced.entries()]));
  }, 3000);
}

function nameFor(jid, pushName) {
  const info = synced.get(jid);
  if (info?.name) return info.name;
  const phone = normalizePhone(jid.split('@')[0]);
  const known = kiraContacts().find((contact) => normalizePhone(contact.phone) === phone);
  return known?.name || pushName || info?.notify || (phone ? `+${phone}` : jid);
}

function onMessages({ messages, type }) {
  for (const msg of messages || []) {
    const jid = msg.key?.remoteJidAlt?.endsWith('@s.whatsapp.net') ? msg.key.remoteJidAlt : msg.key?.remoteJid;
    if (!jid || jid === 'status@broadcast' || isJidGroup(jid)) continue; // grupos e status não viram aviso
    const text = messageText(msg.message);
    if (!text) continue;
    if (msg.pushName && !msg.key.fromMe) rememberContacts([{ id: jid, notify: msg.pushName }]);
    const seconds = Number(msg.messageTimestamp || 0) || Date.now() / 1000;
    noteActivity(jid, seconds);
    const item = { id: msg.key.id, contact: nameFor(jid, msg.key.fromMe ? '' : msg.pushName), jid, text, fromMe: Boolean(msg.key.fromMe), timestamp: new Date(seconds * 1000).toISOString() };
    // conversa do Chefe com o próprio número (bloco de notas): vira conversa com a Kira
    // (Baileys 7: o chat próprio pode chegar como '@lid'; a resposta vai sempre para numero@s.whatsapp.net)
    if (isOwnChat(msg.key, sock?.user)) {
      if (!msg.key.fromMe || ownSent.has(msg.key.id) || ownSentText.has(text) || type !== 'notify') continue; // resposta da Kira: sem loop
      const own = phoneJid(phoneFromJid(sock.user.id));
      const self = { ...item, jid: own, contact: 'Chefe', fromMe: false, self: true };
      recent.push(self); // libera o replyTo (wroteRecently) no próprio número
      secretaryInbox.push({ ...self, kira: 'Chefe' });
      continue;
    }
    // foto, figurinha e vídeo (nunca visualização única) baixados para a Kira ver; até 16 MB
    const media = msg.message?.imageMessage || msg.message?.stickerMessage || msg.message?.videoMessage;
    if (media && !msg.key.fromMe && Number(media.fileLength || 0) < 16 * 1024 * 1024) {
      item.mediaPending = downloadMediaMessage(msg, 'buffer', {}).then((buf) => {
        const ext = msg.message.videoMessage ? 'mp4' : msg.message.stickerMessage ? 'webp' : 'jpg';
        const file = path.join(MEDIA_IN_DIR, `${msg.key.id}.${ext}`);
        fs.mkdirSync(MEDIA_IN_DIR, { recursive: true });
        fs.writeFileSync(file, buf);
        return file;
      }).catch(() => '');
    }
    recent.push({ ...item, mediaPending: undefined });
    if (type !== 'notify' || ownSent.has(msg.key.id)) continue;
    const listed = kiraContactFor(jid, { kiraContacts: kiraContacts(), synced });
    if (listed) {
      if (item.mediaPending) item.mediaPending.then((file) => { const { mediaPending, ...rest } = item; secretaryInbox.push({ ...rest, kira: listed, media: file }); });
      else secretaryInbox.push({ ...item, kira: listed });
    }
    if (!msg.key.fromMe) {
      uiInbox.push(item);
      agentInbox.push(item);
      log(`mensagem de ${item.contact}`);
    }
  }
}

async function connect() {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const { state: auth, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  let version;
  try {
    ({ version } = await fetchLatestBaileysVersion());
  } catch {
    version = undefined; // usa a versão embutida no Baileys
  }
  state.status = 'connecting';
  sock = makeWASocket({
    auth,
    version,
    logger: pino({ level: 'silent' }),
    browser: Browsers.windows('Kira'),
    markOnlineOnConnect: false, // o celular continua recebendo as notificações
    syncFullHistory: false,
  });
  sock.ev.on('creds.update', saveCreds);
  sock.ev.on('contacts.upsert', rememberContacts);
  sock.ev.on('messaging-history.set', onHistory);
  sock.ev.on('contacts.update', rememberContacts);
  sock.ev.on('messages.upsert', onMessages);
  // ligação recebida: avisa a secretária como ligação (antes podia virar '[figurinha]')
  sock.ev.on('call', (calls) => {
    for (const call of calls || []) {
      if (call.status !== 'offer' || !call.from || isJidGroup(call.from)) continue;
      const jid = call.from;
      const item = { id: `call-${call.id}`, contact: nameFor(jid, ''), jid, text: call.isVideo ? '[ligação de vídeo]' : '[ligação]', fromMe: false, timestamp: new Date().toISOString() };
      recent.push(item);
      const listed = kiraContactFor(jid, { kiraContacts: kiraContacts(), synced });
      if (listed) secretaryInbox.push({ ...item, kira: listed });
      uiInbox.push(item);
    }
  });
  sock.ev.on('connection.update', async (update) => {
    if (update.qr) {
      state.status = 'waiting_qr';
      state.qr = await QRCode.toDataURL(update.qr, { margin: 1, width: 288 });
      log('QR novo: escaneie em WhatsApp > Aparelhos conectados');
    }
    if (update.connection === 'open') {
      attempts = 0;
      state.status = 'connected';
      state.qr = null;
      state.lastError = null;
      // o número vai para a aba WhatsApp mascarado (perfil sem nome, como ".", aparece pelo número)
      state.me = { name: sock.user?.name || '', phone: phoneFromJid(sock.user?.id) || '' };
      log('WhatsApp conectado');
    }
    if (update.connection === 'close' && !stopping) {
      const code = update.lastDisconnect?.error?.output?.statusCode;
      state.lastError = update.lastDisconnect?.error?.message || null;
      if (code === DisconnectReason.loggedOut) {
        log('sessão encerrada no celular: um QR novo vai aparecer');
        fs.rmSync(AUTH_DIR, { recursive: true, force: true });
        state.status = 'logged_out';
        setTimeout(connect, 1000);
        return;
      }
      attempts += 1;
      const wait = Math.min(30, 2 ** Math.min(attempts, 5));
      state.status = 'reconnecting';
      log(`conexão caiu (${code ?? 'sem código'}); tentando de novo em ${wait}s`);
      setTimeout(connect, wait * 1000);
    }
  });
}

const SELF_NAMES = /^(eu|mim|meu (proprio )?numero|meu número|meu próprio número|minha conversa|minhas notas|bloco de notas|voce|você)$/i;

async function target(contact) {
  // "manda pro meu número / salva na minha conversa": o chat do Chefe com ele mesmo
  if (SELF_NAMES.test(String(contact || '').trim()) && sock?.user?.id) return { jid: phoneJid(phoneFromJid(sock.user.id)), name: 'Você (meu número)' };
  const found = resolveContact(contact, { kiraContacts: kiraContacts(), synced });
  if (found.error) return found;
  if (found.phone) {
    const [check] = (await sock.onWhatsApp(found.phone)) || [];
    if (!check?.exists) return { error: 'esse número não tem WhatsApp', candidates: [] };
    return { ...found, jid: check.jid || phoneJid(found.phone) };
  }
  return found;
}

async function send(contact, content, label) {
  if (state.status !== 'connected' || !sock) return [503, { success: false, message: 'WhatsApp não está conectado.' }];
  const to = await target(contact);
  if (to.error) {
    // contato ambíguo: a mensagem já traz as opções ("Breno final 1234, Breno final 5678")
    return [404, { success: false, message: to.candidates.length ? to.error : `${to.error}: ${contact}`, candidates: to.candidates }];
  }
  noteSentText(content?.text || content?.caption);
  remember(await sock.sendMessage(to.jid, content));
  recent.push({ id: `out-${Date.now()}`, contact: to.name, jid: to.jid, text: label, fromMe: true, timestamp: new Date().toISOString() });
  log(`enviado para ${to.name}`);
  return [200, { success: true, message: 'Mensagem enviada.', contact: to.name }];
}

/** Texto recém-enviado vale 2 min como "foi a Kira" (evita loop no chat próprio). */
function noteSentText(text) {
  if (!text) return;
  ownSentText.add(text);
  setTimeout(() => ownSentText.delete(text), 120000).unref?.();
}

/** Guarda o id do que a ponte mandou (para não confundir com o Chefe respondendo pelo celular). */
function remember(sent) {
  if (!sent?.key?.id) return;
  ownSent.add(sent.key.id);
  if (ownSent.size > 500) ownSent.delete(ownSent.values().next().value);
}

/** Resposta da secretária: só no chat de quem escreveu há pouco (nunca puxa conversa com outro número). */
function wroteRecently(jid) {
  const since = Date.now() - REPLY_WINDOW_MS;
  return recent.items.some((item) => item.jid === jid && !item.fromMe && Date.parse(item.timestamp) >= since);
}

/** "digitando..." / "gravando áudio..." enquanto a secretária prepara a resposta (só em quem escreveu há pouco). */
async function presence(jid, kind) {
  if (!jid || !wroteRecently(jid) || state.status !== 'connected' || !sock) return [403, { success: false }];
  await sock.sendPresenceUpdate(kind === 'recording' ? 'recording' : kind === 'paused' ? 'paused' : 'composing', jid);
  return [200, { success: true }];
}

async function replyTo(jid, text, audio = '') {
  if (!jid || !text) return [400, { success: false, message: 'faltou o chat ou o texto' }];
  if (!wroteRecently(jid)) return [403, { success: false, message: 'só respondo a quem escreveu há pouco' }];
  if (state.status !== 'connected' || !sock) return [503, { success: false, message: 'WhatsApp não está conectado.' }];
  // áudio da Kira (.ogg Opus) vai como mensagem de voz; o texto fica só no histórico
  const bad = audio ? checkFile(audio) || (path.extname(audio).toLowerCase() === '.ogg' ? null : 'áudio precisa ser .ogg') : null;
  if (bad) return [400, { success: false, message: bad }];
  noteSentText(text);
  remember(await sock.sendMessage(jid, audio ? { audio: { url: audio }, mimetype: 'audio/ogg; codecs=opus', ptt: true } : { text }));
  const name = nameFor(jid, '');
  recent.push({ id: `out-${Date.now()}`, contact: name, jid, text, fromMe: true, timestamp: new Date().toISOString() });
  log(`secretária respondeu ${name}`);
  return [200, { success: true }];
}

function mediaContent(file, caption) {
  const kind = mediaKind(file);
  const ext = path.extname(file).slice(1).toLowerCase();
  if (kind === 'image') return { image: { url: file }, caption };
  if (kind === 'video') return { video: { url: file }, caption };
  if (kind === 'audio') return { audio: { url: file }, mimetype: AUDIO_MIME[ext] || 'audio/mpeg' };
  return { document: { url: file }, fileName: path.basename(file), mimetype: 'application/octet-stream', caption };
}

function checkFile(file) {
  if (!file || !path.isAbsolute(file)) return 'caminho do arquivo precisa ser completo';
  let info;
  try {
    info = fs.statSync(file);
  } catch {
    return 'arquivo não encontrado';
  }
  if (!info.isFile()) return 'não é um arquivo';
  if (info.size > MAX_MEDIA_BYTES) return 'arquivo maior que 64 MB';
  return null;
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.setEncoding('utf8');
    req.on('data', (chunk) => {
      body += chunk;
      if (body.length > 1_000_000) reject(new Error('corpo grande demais'));
    });
    req.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch {
        reject(new Error('JSON inválido'));
      }
    });
  });
}

function reply(res, code, payload) {
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(payload));
}

function statusPayload() {
  const connected = state.status === 'connected';
  return {
    connected, controller_active: Boolean(sock), state: state.status, engine: 'baileys', qr: state.qr, me: state.me,
    last_error: state.lastError, window_visible: false, window_state: 'headless',
  };
}

async function shutdown(code = 0) {
  stopping = true;
  try {
    sock?.end(undefined);
  } catch {
    // já fechado
  }
  setTimeout(() => process.exit(code), 200);
}

const routes = {
  'GET /status': async () => [200, statusPayload()],
  // O agente manda o número que achou na lista da Kira; sem ele, o nome é procurado aqui.
  'POST /send': async (body) => send(body.number || body.contact, { text: String(body.message || '') }, String(body.message || '')),
  'POST /send-media': async (body) => {
    const problem = checkFile(body.path);
    if (problem) return [400, { success: false, message: `${problem}: ${body.path}` }];
    return send(body.contact, mediaContent(body.path, String(body.caption || '')), `[${mediaKind(body.path)}] ${path.basename(body.path)}`);
  },
  'GET /messages/new': async () => [200, { messages: uiInbox.drain() }],
  'GET /messages/new/agent': async () => [200, { messages: agentInbox.drain() }],
  'GET /messages/new/secretary': async () => [200, { messages: secretaryInbox.drain() }],
  'POST /reply': async (body) => replyTo(String(body.jid || ''), String(body.message || '').trim(), String(body.audio || '')),
  'POST /presence': async (body) => presence(String(body.jid || ''), String(body.kind || '')),
  'GET /messages/recent': async (_, query) => {
    const limit = Math.min(Number(query.get('limit')) || 10, 50);
    const wanted = query.get('contact');
    let items = recent.items;
    if (wanted) {
      const to = resolveContact(wanted, { kiraContacts: kiraContacts(), synced });
      if (to.error) return [404, { messages: [], message: `${to.error}: ${wanted}` }];
      const phone = to.phone || normalizePhone(to.jid.split('@')[0]);
      items = items.filter((item) => item.jid === to.jid || (phone && item.jid.startsWith(phone)));
    }
    return [200, { messages: items.slice(-limit) }];
  },
  'GET /log': async () => [200, { log: [...logLines] }],
  'POST /logout': async () => {
    try {
      await sock?.logout();
    } catch {
      // a sessão pode já ter caído
    }
    return [200, { ok: true, state: 'logged_out' }];
  },
  'POST /shutdown': async () => {
    shutdown(0);
    return [200, { ok: true, state: 'stopped' }];
  },
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const route = routes[`${req.method} ${url.pathname}`];
  if (!route) return reply(res, 404, { error: 'rota não existe' });
  if (url.pathname !== '/status' && TOKEN && req.headers['x-kira-internal'] !== TOKEN) {
    return reply(res, 403, { error: 'ação interna não autorizada' });
  }
  try {
    const body = req.method === 'POST' ? await readJson(req) : {};
    const [code, payload] = await route(body, url.searchParams);
    reply(res, code, payload);
  } catch (error) {
    log(`erro em ${url.pathname}: ${error.message}`);
    reply(res, 500, { success: false, message: error.message });
  }
});

// Porta 5100: o kira_app chama /start_bridge depois de subir o processo.
const admin = http.createServer((req, res) => {
  if (req.url !== '/start_bridge') return reply(res, 404, { error: 'rota não existe' });
  reply(res, 200, { status: state.status === 'connected' ? 'already_connected' : 'started', state: state.status });
});

for (const signal of ['SIGINT', 'SIGTERM', 'SIGBREAK']) process.on(signal, () => shutdown(0));
process.on('unhandledRejection', (error) => log(`erro não tratado: ${error?.message || error}`));

server.listen(PORT, '127.0.0.1', () => log(`HTTP em 127.0.0.1:${PORT}`));
admin.listen(ADMIN_PORT, '127.0.0.1');
if (!TOKEN) log('aviso: KIRA_INTERNAL_TOKEN ausente; rotas abertas a qualquer processo local');
connect().catch((error) => {
  state.status = 'error';
  state.lastError = error.message;
  log(`falha ao iniciar: ${error.message}`);
});
