// Configurações > Celular: liga o acesso pelo celular, senha, rede de casa, link público, perfis, "sair de todos" e os links. Só funciona na
// tela do próprio PC (pelo celular as rotas recusam e a aba avisa). Tudo por textContent.

const MIN_PASSWORD = 8;

/** Texto de ajuda: como entrar em casa (mesmo Wi-Fi) e de qualquer lugar (Tailscale, link fixo com HTTPS). */
export function helpText(status = {}) {
  const tailscale = status.tailscale || {};
  if (!status.has_password) return 'Primeiro crie uma senha (mínimo 8 caracteres). Depois ligue.';
  if (!status.enabled) return status.lan ? 'Desligado. Ligue para o link de casa funcionar.' : 'Desligado: ninguém entra pelo celular.';
  if (status.error) return `O acesso não abriu: ${status.error}.`;
  const parts = [];
  if (status.links?.casa) {
    parts.push('Em casa: com o celular no mesmo Wi-Fi do PC, aponte a câmera para o QR (ou digite o endereço) e entre com a senha. '
      + 'Se o Windows perguntar, permita o acesso em "redes privadas".');
  }
  if (!tailscale.installed) {
    parts.push(status.public_link
      ? 'De qualquer lugar (com HTTPS e microfone): instale o Tailscale só no PC (tailscale.com, grátis) e entre com uma conta. '
        + 'Depois volte aqui: o link fixo aparece sozinho e abre no celular pelos dados móveis, sem app.'
      : 'De qualquer lugar (com HTTPS e microfone): instale o Tailscale no PC e no celular (tailscale.com, grátis) e entre '
        + 'com a mesma conta nos dois. Ou ligue "Link público": aí o Tailscale vai só no PC.');
  } else if (!tailscale.running) {
    parts.push('O Tailscale está instalado, mas desligado ou sem conta: abra o Tailscale no PC e entre.');
  } else if (tailscale.error) {
    parts.push(String(tailscale.error).startsWith('libere o Funnel')
      ? `Falta um passo no Tailscale: ${tailscale.error} (só uma vez). Depois desligue e ligue "Link público".`
      : `O Tailscale não deixou criar o link: ${tailscale.error}. No painel do Tailscale, ligue "HTTPS Certificates".`);
  } else if (status.public_link) {
    parts.push('De qualquer lugar: abra o link fixo no celular (dados móveis ou qualquer Wi-Fi, sem app) e entre com a senha. '
      + 'Ele é HTTPS: o microfone funciona.');
  } else {
    parts.push('De qualquer lugar: abra o link fixo no celular (com o Tailscale ligado nele) e entre com a senha.');
  }
  return parts.join(' ');
}

/** Linha de status curta. */
export function statusText(status = {}) {
  if (status.forbidden) return 'Só no PC: daqui do celular você usa a Kira, mas não muda este acesso.';
  if (!status.enabled) return status.has_password ? 'Desligado.' : 'Sem senha ainda.';
  if (!status.running) return status.error ? `Não abriu: ${status.error}.` : 'Ligando…';
  const count = Object.keys(status.links || {}).length;
  return count ? `Ligado. ${count === 1 ? '1 link' : `${count} links`} abaixo.` : 'Ligado, mas ainda sem link: veja ao lado.';
}

export function createRemoteAccess({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  const doc = documentRef;
  const $ = (id) => doc.getElementById(id);
  const panel = $('config-panel-remote');
  let current = {};

  const say = (id, text) => { const node = $(id); if (node) node.textContent = text; };
  const request = async (path, body) => {
    const response = await windowRef.fetch(path, body
      ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), cache: 'no-store' }
      : { cache: 'no-store' });
    if (response.status === 403) return { forbidden: true };
    return response.json();
  };

  function setSwitch(id, on, disabled) {
    const node = $(id);
    if (!node) return;
    node.classList.toggle('enabled', Boolean(on));
    node.setAttribute('aria-checked', on ? 'true' : 'false');
    node.disabled = Boolean(disabled);
  }

  function renderLinks(links = {}, preview = '') {
    const box = $('remote-links');
    if (!box) return;
    const labels = { fixo: 'Link fixo (de qualquer lugar)', casa: 'Em casa (mesmo Wi-Fi)' };
    const entries = Object.entries(links);
    // "Rede de casa" marcada com o acesso ainda desligado: mostra qual vai ser o endereço
    if (preview && !links.casa) entries.push(['casa', preview]);
    const rows = entries.map(([kind, url]) => {
      const row = doc.createElement('div');
      row.className = 'remote-link';
      const text = doc.createElement('div');
      const title = doc.createElement('b');
      title.textContent = labels[kind] || kind;
      const address = doc.createElement('span');
      address.textContent = String(url);
      text.append(title, address);
      const copy = doc.createElement('button');
      copy.type = 'button';
      copy.className = 'kira-settings-chip';
      copy.textContent = 'Copiar';
      copy.addEventListener('click', async () => {
        try {
          await windowRef.navigator?.clipboard?.writeText(String(url));
          copy.textContent = 'Copiado';
        } catch (_) {
          copy.textContent = 'Não copiou';
        }
        windowRef.setTimeout(() => { copy.textContent = 'Copiar'; }, 1800);
      });
      const qr = doc.createElement('img');
      qr.className = 'remote-qr';
      qr.alt = `QR code: ${labels[kind] || kind}`;
      qr.src = `/api/remote/qr?kind=${encodeURIComponent(kind)}&t=${Date.now()}`;
      qr.addEventListener('error', () => qr.remove());  // sem QR (link mudou, gerador ausente): fica só o endereço
      row.append(qr, text, copy);
      return row;
    });
    if (!rows.length) {
      const empty = doc.createElement('div');
      empty.className = 'kira-settings-help';
      empty.textContent = 'Nenhum link ainda.';
      rows.push(empty);
    }
    box.replaceChildren(...rows);
  }

  function render(status = {}) {
    current = status;
    const locked = Boolean(status.forbidden);
    setSwitch('remote-enabled', status.enabled, locked || !status.has_password);
    setSwitch('remote-lan', status.lan, locked);
    setSwitch('remote-public', status.public_link, locked);
    for (const id of ['remote-password', 'remote-logout-all']) { const node = $(id); if (node) node.disabled = locked; }
    say('remote-status', statusText(status));
    say('remote-help', locked ? '' : helpText(status));
    // link do Tailscale para liberar o Funnel: botão que abre no navegador do PC (antes era texto para copiar)
    const enable = String(status.tailscale?.error || '').match(/https:\/\/login\.tailscale\.com\/\S+/);
    const help = $('remote-help');
    if (enable && help && !locked) {
      const open = doc.createElement('button');
      open.type = 'button';
      open.className = 'kira-settings-chip remote-open-link';
      open.textContent = 'Abrir no navegador';
      open.addEventListener('click', () => request('/api/ui/open-external', { url: enable[0] }).catch(() => {}));
      help.append(doc.createElement('br'), open);
    }
    renderLinks(locked ? {} : status.links, locked || !status.lan ? '' : status.lan_preview);
  }

  async function refresh() {
    try {
      const payload = await request('/api/remote/settings');
      render(payload?.forbidden ? { forbidden: true } : payload?.data || {});
    } catch (_) {
      say('remote-status', 'Não consegui ler o acesso agora.');
    }
  }

  async function save(body, note = '') {
    say('remote-status', 'Salvando…');
    try {
      const payload = await request('/api/remote/settings', body);
      if (payload?.forbidden) return render({ forbidden: true });
      if (!payload?.success) {
        say('remote-status', payload?.error || 'Não consegui salvar.');
        return;
      }
      render(payload.data || {});
      if (note) say('remote-status', note);
    } catch (_) {
      say('remote-status', 'Não consegui salvar.');
    }
  }

  $('remote-enabled')?.addEventListener('click', () => save({ enabled: !current.enabled }));
  $('remote-lan')?.addEventListener('click', () => save({ lan: !current.lan }));
  $('remote-public')?.addEventListener('click', () => save({ public_link: !current.public_link }));
  $('remote-password-form')?.addEventListener('submit', (event) => {
    event?.preventDefault?.();
    const input = $('remote-password');
    const password = String(input?.value || '');
    if (password.length < MIN_PASSWORD) {
      say('remote-status', `A senha precisa de pelo menos ${MIN_PASSWORD} caracteres.`);
      return;
    }
    if (input) input.value = '';
    save({ password }, 'Senha salva. Quem estava conectado precisa entrar de novo.');
  });
  $('remote-logout-all')?.addEventListener('click', async () => {
    try {
      const payload = await request('/api/remote/logout-all', {});
      if (payload?.data) render(payload.data);
      say('remote-status', 'Pronto: todos os aparelhos precisam entrar de novo.');
    } catch (_) {
      say('remote-status', 'Não consegui agora.');
    }
  });

  // ---------- perfis (até 3; o do Chefe é o dono) ----------
  const send = async (method, path, body) => {
    const response = await windowRef.fetch(path, { method, headers: { 'Content-Type': 'application/json' }, cache: 'no-store',
      body: body === undefined ? undefined : JSON.stringify(body) });
    return response.status === 403 ? { forbidden: true } : response.json();
  };

  function showTalk(payload) {
    const box = $('remote-profile-view');
    if (!box) return;
    const rows = [];
    const title = doc.createElement('b');
    title.textContent = `Conversa com ${payload.name || ''}`;
    rows.push(title);
    for (const line of (payload.lines || []).slice(-60)) {
      const row = doc.createElement('div');
      row.className = 'remote-profile-line';
      row.textContent = `${line.who === 'kira' ? 'Kira' : payload.name}: ${line.text}`;
      rows.push(row);
    }
    if ((payload.facts || []).length) {
      const facts = doc.createElement('div');
      facts.className = 'kira-settings-help';
      facts.textContent = `A Kira anotou: ${payload.facts.join('; ')}`;
      rows.push(facts);
    }
    if (rows.length === 1) {
      const empty = doc.createElement('div');
      empty.className = 'kira-settings-help';
      empty.textContent = 'Nenhuma conversa ainda.';
      rows.push(empty);
    }
    box.replaceChildren(...rows);
    box.hidden = false;
  }

  // o que cada perfil comum pode usar (o dono tem tudo); WhatsApp, agenda e e-mail da própria pessoa ainda não
  const FEATURES = [['voz', 'Voz da Kira'], ['clima', 'Clima'], ['noticias', 'Notícias e resumo'], ['cozinha', 'Cozinha'],
    ['musica', 'Música'], ['lembretes', 'Lembretes']];
  function featureSwitches(profile) {
    const box = doc.createElement('div');
    box.className = 'remote-profile-features';
    const current = { ...(profile.features || {}) };
    for (const [key, label] of FEATURES) {
      const chip = doc.createElement('button');
      chip.type = 'button';
      chip.className = `kira-settings-chip remote-feature${current[key] ? ' is-on' : ''}`;
      chip.textContent = label;
      chip.setAttribute('aria-pressed', current[key] ? 'true' : 'false');
      chip.addEventListener('click', async () => {
        const payload = await send('POST', `/api/remote/profiles/${encodeURIComponent(profile.id)}/features`, { features: { [key]: !current[key] } });
        if (payload?.success) renderProfiles(payload.profiles || []);
        else say('remote-status', payload?.error || 'Não consegui mudar.');
      });
      box.append(chip);
    }
    const later = doc.createElement('span');
    later.className = 'kira-settings-help';
    later.textContent = 'WhatsApp, agenda e e-mail da própria pessoa: ainda não (cada um precisa conectar a conta dele).';
    box.append(later);
    return box;
  }

  function renderProfiles(list = []) {
    const box = $('remote-profiles');
    if (!box) return;
    const rows = list.map((profile) => {
      const row = doc.createElement('div');
      row.className = 'remote-profile';
      const name = doc.createElement('div');
      const title = doc.createElement('b');
      title.textContent = profile.name;
      const role = doc.createElement('span');
      role.textContent = profile.role === 'owner' ? 'Dono · vê tudo' : 'Perfil comum';
      name.append(title, role);
      const actions = doc.createElement('div');
      actions.className = 'remote-profile-actions';
      const button = (label, onClick) => {
        const node = doc.createElement('button');
        node.type = 'button';
        node.className = 'kira-settings-chip';
        node.textContent = label;
        node.addEventListener('click', onClick);
        actions.append(node);
        return node;
      };
      const passwordForm = doc.createElement('form');
      passwordForm.className = 'remote-profile-password';
      passwordForm.hidden = true;
      const field = doc.createElement('input');
      field.type = 'password';
      field.className = 'kira-settings-field';
      field.placeholder = 'Senha nova (mínimo 8)';
      field.autocomplete = 'new-password';
      const ok = doc.createElement('button');
      ok.type = 'submit';
      ok.className = 'kira-settings-chip';
      ok.textContent = 'Salvar';
      passwordForm.append(field, ok);
      passwordForm.addEventListener('submit', async (event) => {
        event?.preventDefault?.();
        const payload = await send('POST', `/api/remote/profiles/${encodeURIComponent(profile.id)}/password`, { password: String(field.value || '') });
        field.value = '';
        const who = profile.role === 'owner' ? 'todo aparelho' : 'esse perfil';
        say('remote-status', payload?.success ? `Senha de ${profile.name} trocada: ${who} entra de novo.` : (payload?.error || 'Não consegui trocar.'));
        if (payload?.success) passwordForm.hidden = true;
      });
      button('Trocar senha', () => { passwordForm.hidden = !passwordForm.hidden; });
      if (profile.role !== 'owner') {
        button('Conversa', async () => showTalk(await send('GET', `/api/remote/profiles/${encodeURIComponent(profile.id)}/conversa`)));
        button('Remover', async () => {
          const payload = await send('DELETE', `/api/remote/profiles/${encodeURIComponent(profile.id)}`);
          renderProfiles(payload?.profiles || []);
          say('remote-status', payload?.success ? `Perfil ${profile.name} removido (com a memória dele).` : 'Não consegui remover.');
        });
      }
      row.append(name, actions, passwordForm);
      if (profile.role !== 'owner') row.append(featureSwitches(profile));
      return row;
    });
    box.replaceChildren(...rows);
    const form = $('remote-profile-form');
    if (form) form.hidden = list.length >= 3;
  }

  async function refreshProfiles() {
    try {
      const payload = await send('GET', '/api/remote/profiles');
      if (!payload?.forbidden) renderProfiles(payload?.profiles || []);
    } catch (_) { /* a aba abre sem a lista */ }
  }

  $('remote-profile-form')?.addEventListener('submit', async (event) => {
    event?.preventDefault?.();
    const name = $('remote-profile-name');
    const password = $('remote-profile-password');
    const payload = await send('POST', '/api/remote/profiles', { name: String(name?.value || ''), password: String(password?.value || '') });
    if (payload?.success) {
      if (name) name.value = '';
      if (password) password.value = '';
    }
    renderProfiles(payload?.profiles || []);
    say('remote-status', payload?.success ? 'Perfil criado: ele aparece na tela de entrar do celular.' : (payload?.error || 'Não consegui criar.'));
  });

  // atualiza quando a aba Celular aparece
  if (panel && windowRef.MutationObserver) {
    new windowRef.MutationObserver(() => { if (!panel.hidden) { refresh(); refreshProfiles(); } }).observe(panel, { attributes: true, attributeFilter: ['hidden'] });
  }
  return { refresh, render, save, renderProfiles, refreshProfiles };
}

if (globalThis.document?.getElementById?.('config-panel-remote') && globalThis.window && !globalThis.window.KiraRemoteAccess) {
  globalThis.window.KiraRemoteAccess = createRemoteAccess();
}
