// Casa inteligente (menu hambúrguer), 2026-10-31: em cima só os aparelhos que o Chefe adicionou, cada um num cartão com
// os controles grandes (TV LG: ligar, desligar, volume, mudo, YouTube e apps). A rede inteira (o que a Kira achou no
// Wi-Fi) fica escondida em "Adicionar aparelho": de lá, um toque em "Adicionar" dá o nome e ele passa para cima.

const KIND_ICON = {
  'TV LG': 'tv', TV: 'tv', 'Chromecast / TV': 'tv', 'roteador / Wi-Fi': 'wifi', 'celular / tablet': 'phone', impressora: 'printer',
  'caixa de som': 'speaker', 'casa inteligente': 'bulb', 'adicionado à mão': 'plus', aparelho: 'plug',
};
const ICONS = {
  tv: '<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8M12 17v4"/>',
  wifi: '<path d="M2 9a15 15 0 0 1 20 0M5 13a10 10 0 0 1 14 0M8.5 16.5a5 5 0 0 1 7 0"/><circle cx="12" cy="20" r="1"/>',
  phone: '<rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/>',
  printer: '<path d="M6 9V3h12v6M6 18H4v-7h16v7h-2M8 14h8v7H8z"/>',
  speaker: '<rect x="5" y="2" width="14" height="20" rx="2"/><circle cx="12" cy="14" r="4"/><circle cx="12" cy="6" r="1"/>',
  bulb: '<path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7V17h8v-2.3A7 7 0 0 0 12 2Z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>', plug: '<path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4"/>',
  power: '<path d="M12 2v10M6.3 6.3a8 8 0 1 0 11.4 0"/>', volDown: '<path d="M11 5 6 9H3v6h3l5 4z"/><path d="M16 12h5"/>',
  volUp: '<path d="M11 5 6 9H3v6h3l5 4z"/><path d="M16 12h5M18.5 9.5v5"/>', mute: '<path d="M11 5 6 9H3v6h3l5 4z"/><path d="m22 9-6 6M16 9l6 6"/>',
  play: '<path d="m7 4 13 8-13 8z"/>', house: '<path d="M3 11 12 3l9 8M5 9.5V21h5v-6h4v6h5V9.5"/>', yt: '<rect x="2" y="5" width="20" height="14" rx="4"/><path d="m10 9 5 3-5 3z"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
};
const CONTROLS = {
  lgtv: [['ligar', 'Ligar', 'power'], ['desligar', 'Desligar', 'power'], ['diminuir', 'Volume −', 'volDown'], ['aumentar', 'Volume +', 'volUp'], ['mudo', 'Mudo', 'mute']],
  chromecast: [['tocar', 'Tocar', 'play'], ['pausar', 'Pausar', 'play'], ['mudo', 'Mudo', 'mute'], ['parar', 'Parar', 'power']],
};

function el(doc, tag, cls = '', text = '') {
  const node = doc.createElement(tag);
  if (cls) node.className = cls;
  if (text) node.textContent = text;
  return node;
}
function icon(doc, name) {
  const span = el(doc, 'span', 'kui-home-i');
  span.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ICONS.plug}</svg>`;
  return span;
}

export function createSmartHomePanel() {
  return {
    mount(container) {
      const doc = container.ownerDocument || globalThis.document;
      const win = doc.defaultView || globalThis.window;
      const root = el(doc, 'div', 'kui-home');
      const head = el(doc, 'div', 'kui-home-top');
      const title = el(doc, 'b', '', 'Meus aparelhos');
      const status = el(doc, 'span', 'kui-home-status', '');
      const addToggle = el(doc, 'button', 'kui-home-addbtn', '+ Adicionar aparelho');
      addToggle.type = 'button';
      head.append(title, status, addToggle);
      const mine = el(doc, 'div', 'kui-home-mine');
      const drawer = el(doc, 'section', 'kui-home-drawer');
      drawer.hidden = true;
      const drawerBar = el(doc, 'div', 'kui-home-drawerbar');
      const drawerNote = el(doc, 'span', '', 'Aparelhos na rede de casa');
      const rescan = el(doc, 'button', 'kui-panel-button', 'Procurar de novo');
      rescan.type = 'button';
      drawerBar.append(drawerNote, rescan);
      const manual = el(doc, 'form', 'kui-home-add');
      const ipInput = el(doc, 'input');
      ipInput.placeholder = 'IP (ex.: 192.168.0.50)';
      ipInput.id = 'kui-home-add-ip';
      const nameInput = el(doc, 'input');
      nameInput.placeholder = 'Nome (ex.: TV da sala)';
      nameInput.id = 'kui-home-add-name';
      const manualButton = el(doc, 'button', 'kui-panel-button', 'Adicionar pelo IP');
      manualButton.type = 'submit';
      manual.append(ipInput, nameInput, manualButton);
      const network = el(doc, 'div', 'kui-home-network');
      drawer.append(drawerBar, network, manual);
      const left = el(doc, 'div', 'kui-home-left');
      const side = el(doc, 'aside', 'kui-home-side');
      left.append(head, mine);
      side.append(drawer);
      root.append(left, side);
      container.appendChild(root);

      const api = async (path, body, limitMs = 15000) => {
        const stop = new win.AbortController();
        const timer = win.setTimeout(() => stop.abort(), limitMs);
        try {
          const response = await win.fetch(path, body === undefined ? { cache: 'no-store', signal: stop.signal }
            : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: stop.signal });
          return await response.json();
        } finally {
          win.clearTimeout(timer);
        }
      };

      function card(device) {
        const box = el(doc, 'article', `kui-home-card${device.control ? ' is-controllable' : ''}`);
        const top = el(doc, 'div', 'kui-home-card-top');
        const badge = icon(doc, KIND_ICON[device.kind] || 'plug');
        const naming = el(doc, 'div', 'kui-home-naming');
        const name = el(doc, 'input', 'kui-home-name');
        name.value = device.nickname || device.name || '';
        name.placeholder = device.label || device.ip;
        name.setAttribute('aria-label', `Nome de ${device.ip}`);
        name.title = 'Mude o nome e aperte Enter';
        const save = async () => {
          if ((device.nickname || '') === name.value.trim()) return;
          const answer = await api('/api/casa/renomear', { ip: device.ip, nome: name.value.trim() });
          if (answer.ok) device.nickname = name.value.trim();
          name.classList.add(answer.ok ? 'is-saved' : 'is-error');
          win.setTimeout(() => name.classList.remove('is-saved', 'is-error'), 1200);
        };
        name.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); name.blur(); } });
        name.addEventListener('blur', save);
        const sub = el(doc, 'small', '', [device.kind, device.maker, device.ip].filter(Boolean).join(' · '));
        naming.append(name, sub);
        const dot = el(doc, 'span', 'kui-home-dot');
        dot.title = device.control ? 'A Kira controla' : 'Só vista na rede';
        top.append(badge, naming, dot);
        box.append(top);
        const note = el(doc, 'span', 'kui-home-note');
        const run = async (action, value, button) => {
          if (button) button.disabled = true;
          note.textContent = '…';
          const answer = await api('/api/casa/comando', { ip: device.ip, acao: action, valor: value }, 40000).catch(() => ({ ok: false, message: 'sem resposta' }));
          note.textContent = answer.ok ? (answer.nota || 'Feito.') : (answer.message || 'Não deu.');
          if (button) button.disabled = false;
        };
        if (device.control === 'lgtv') { // botões no controle ao lado: o cartão só escolhe a TV
          box.classList.add('is-tv');
          box.addEventListener('click', (event) => { if (!event.target.closest('input')) showRemote(device); });
        } else if (CONTROLS[device.control]) {
          const controls = el(doc, 'div', 'kui-home-controls');
          CONTROLS[device.control].forEach(([action, label, ic]) => {
            const button = el(doc, 'button', `kui-home-ctl kui-home-ctl-${action}`);
            button.type = 'button';
            button.append(icon(doc, ic), el(doc, 'span', '', label));
            button.addEventListener('click', () => run(action, undefined, button));
            controls.append(button);
          });
          box.append(controls);
          if (device.control === 'lgtv') {
            const yt = el(doc, 'form', 'kui-home-yt');
            const query = el(doc, 'input');
            query.placeholder = 'Tocar no YouTube da TV…';
            query.setAttribute('aria-label', 'Buscar no YouTube da TV');
            const go = el(doc, 'button', 'kui-home-ctl');
            go.type = 'submit';
            go.append(icon(doc, 'yt'), el(doc, 'span', '', 'YouTube'));
            yt.append(query, go);
            yt.addEventListener('submit', (event) => { event.preventDefault(); run('youtube', query.value.trim(), go); });
            const apps = el(doc, 'button', 'kui-home-ctl kui-home-apps');
            apps.type = 'button';
            apps.append(icon(doc, 'grid'), el(doc, 'span', '', 'Apps'));
            const appList = el(doc, 'div', 'kui-home-applist');
            apps.addEventListener('click', async () => {
              apps.disabled = true;
              const answer = await api('/api/casa/comando', { ip: device.ip, acao: 'apps' }, 40000).catch(() => ({ ok: false }));
              apps.disabled = false;
              appList.replaceChildren(...(answer.apps || []).map((title) => {
                const chip = el(doc, 'button', 'kui-home-chip', title);
                chip.type = 'button';
                chip.addEventListener('click', () => run('abrir', title, chip));
                return chip;
              }));
              if (!answer.ok) note.textContent = answer.message || 'A TV não respondeu.';
            });
            yt.append(apps);
            box.append(yt, appList);
          }
        }
        box.append(note);
        return box;
      }

      // Controle remoto da TV LG (Neon de LED): como o Magic Remote — ligar, entrada TV, casinha, números, volume, canal,
      // direcional, voltar/sair e os apps (abertos pelo nome instalado na TV)
      let remote = null;
      function showRemote(device) {
        drawer.hidden = true;
        remote?.remove();
        remote = el(doc, 'section', 'kui-remote');
        const note = el(doc, 'span', 'kui-remote-note', device.nickname || device.label || 'TV');
        const send = async (action, value, button) => {
          button?.classList.add('is-busy');
          const answer = await api('/api/casa/comando', { ip: device.ip, acao: action, valor: value }, 40000).catch(() => ({ ok: false, message: 'sem resposta' }));
          button?.classList.remove('is-busy');
          note.textContent = answer.ok ? (answer.valor ? `▶ ${answer.valor}` : '✓') : (answer.message || 'A TV não respondeu.');
        };
        const key = (label, action, value, cls = '') => {
          const button = el(doc, 'button', `kui-rk ${cls}`, ICONS[label] ? '' : label); // ícone desenhado (a fonte não tem ⏻ e ⌂)
          if (ICONS[label]) button.append(icon(doc, label));
          button.type = 'button';
          button.addEventListener('click', () => send(action, value, button));
          return button;
        };
        const group = (cls, ...keys) => { const g = el(doc, 'div', cls); g.append(...keys); return g; };
        remote.append(
          group('kui-r-top', key('power', 'desligar', '', 'is-power'), key('Ligar', 'ligar', '', 'is-on'), key('TV', 'tv'), key('house', 'home'), key('Lista', 'lista')),
          group('kui-r-nums', ...'123456789'.split('').map((n) => key(n, n)), key('Info', 'info'), key('0', '0'), key('Sair', 'sair')),
          group('kui-r-mid',
            group('kui-r-rock', key('+', 'aumentar'), el(doc, 'small', '', 'VOL'), key('−', 'diminuir')),
            group('kui-r-pad', key('▲', 'cima', '', 'u'), key('◀', 'esquerda', '', 'l'), key('OK', 'ok', '', 'ok'), key('▶', 'direita', '', 'r'), key('▼', 'baixo', '', 'd')),
            group('kui-r-rock', key('+', 'canal+'), el(doc, 'small', '', 'CH'), key('−', 'canal-'))),
          group('kui-r-row', key('🔇', 'mudo'), key('↩', 'voltar'), key('⏯', 'tocar'), key('⏸', 'pausar')),
          group('kui-r-apps', ...[['Netflix', 'nf'], ['Prime Video', 'pv'], ['Disney+', 'dy'], ['Globoplay', 'gp'], ['YouTube', 'yt'], ['Prime IPTV', 'pi'], ['LG Channels', 'lg'], ['Alexa', 'ax']]
            .map(([name, cls]) => key(name, 'abrir', name, `app-${cls}`))));
        const yt = el(doc, 'form', 'kui-r-yt');
        const query = el(doc, 'input');
        query.placeholder = 'Buscar no YouTube da TV…';
        query.id = 'kui-remote-yt';
        const allApps = el(doc, 'button', 'kui-rk', 'Apps');
        allApps.type = 'button';
        const appList = el(doc, 'div', 'kui-r-applist');
        appList.hidden = true;
        allApps.addEventListener('click', async () => {
          if (!appList.hidden) { appList.hidden = true; return; }
          const answer = await api('/api/casa/comando', { ip: device.ip, acao: 'apps' }, 40000).catch(() => ({ ok: false }));
          appList.replaceChildren(...(answer.apps || []).map((title) => key(title, 'abrir', title)));
          appList.hidden = false;
        });
        yt.append(query, allApps);
        yt.addEventListener('submit', (event) => { event.preventDefault(); if (query.value.trim()) send('youtube', query.value.trim()); });
        remote.append(yt, appList, note);
        side.append(remote);
      }

      function row(device, isMine) {
        const line = el(doc, 'div', 'kui-home-row');
        line.append(icon(doc, KIND_ICON[device.kind] || 'plug'));
        const text = el(doc, 'span', '', device.label || device.ip);
        text.append(el(doc, 'small', '', [device.kind, device.ip].filter(Boolean).join(' · ')));
        line.append(text);
        if (isMine) {
          line.append(el(doc, 'em', '', 'adicionado'));
        } else {
          const add = el(doc, 'button', 'kui-panel-button', 'Adicionar');
          add.type = 'button';
          add.addEventListener('click', async () => {
            const nick = device.nickname || device.name || (device.kind === 'TV LG' ? 'TV da sala' : device.label);
            await api('/api/casa/adicionar', { ip: device.ip, nome: nick });
            load(false);
          });
          line.append(add);
        }
        return line;
      }

      async function load(fresh = false) {
        const started = Date.now();
        const orb = doc.createElement('kira-orb'); // mini Thinking Orb procurando na rede
        orb.setAttribute('state', 'buscando'); orb.setAttribute('size', '18'); orb.setAttribute('label', '');
        const tickText = () => { status.replaceChildren(orb); orb.setAttribute('label', `procurando… ${Math.round((Date.now() - started) / 1000)} s`); };
        tickText();
        const ticker = win.setInterval(tickText, 1000);
        rescan.disabled = true;
        try {
          const answer = await api(`/api/casa/dispositivos${fresh ? '?fresh=true' : ''}`, undefined, 90000);
          const all = answer.dispositivos || [];
          const own = answer.meus || [];
          const ownIps = new Set(own.map((device) => device.ip));
          mine.replaceChildren(...(own.length ? own.map(card) : [el(doc, 'p', 'kui-home-empty', 'Nenhum aparelho seu ainda. Toque em "+ Adicionar aparelho" e escolha a TV ou o que quiser controlar.')]));
          network.replaceChildren(...all.map((device) => row(device, ownIps.has(device.ip))));
          status.textContent = own.length ? `${own.length} aparelho${own.length > 1 ? 's' : ''}` : '';
          const tv = own.find((device) => device.control === 'lgtv');
          if (tv && !remote && drawer.hidden) showRemote(tv);
          if (!own.length) drawer.hidden = false;
        } catch (error) {
          status.textContent = error?.name === 'AbortError' ? 'a rede não respondeu' : 'não consegui olhar a rede';
        } finally {
          win.clearInterval(ticker);
          rescan.disabled = false;
        }
      }

      rescan.addEventListener('click', () => load(true));
      addToggle.addEventListener('click', () => { // a lista da rede toma o lugar do controle
        drawer.hidden = !drawer.hidden;
        if (remote) remote.hidden = !drawer.hidden;
      });
      manual.addEventListener('submit', async (event) => {
        event.preventDefault();
        const answer = await api('/api/casa/adicionar', { ip: ipInput.value.trim(), nome: nameInput.value.trim() });
        if (!answer.ok) { status.textContent = answer.message || 'Não deu para adicionar.'; return; }
        ipInput.value = '';
        nameInput.value = '';
        load(false);
      });
      load(false);
      return { unmount() { root.remove(); } };
    },
  };
}
