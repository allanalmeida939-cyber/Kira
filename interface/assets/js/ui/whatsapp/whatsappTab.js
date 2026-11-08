// Aba WhatsApp: números de hoje, envio rápido com confirmação e os contatos da Kira (adicionar e remover). Conta, QR,
// serviço e modo continuam com as funções da página (waUpdateLoginArea, waSetMode). Tudo por textContent: nomes vêm
// do WhatsApp e da agenda da Kira.

export const QUICK_REPLIES = Object.freeze(['Tô chegando', 'Já te ligo', 'Em reunião, falo depois', 'Ok! 👍', 'Bom dia!']);
const REFRESH_MS = 15000;
const CONFIRM_REMOVE_MS = 3000;

function el(doc, tag, className = '', text = '') {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

/** "Mãe" -> "M"; "+55 85 9…" -> "#". */
export function initial(name) {
  const letter = String(name || '').trim().match(/\p{L}/u);
  return letter ? letter[0].toUpperCase() : '#';
}

/** Só os dígitos; DDD + número (10 ou 11 dígitos, sem "+") ganha o 55 do Brasil. Com "+" é internacional como veio. */
export function contactDigits(value) {
  const raw = String(value || '').trim();
  const digits = raw.replace(/\D/g, '');
  return !raw.startsWith('+') && (digits.length === 10 || digits.length === 11) ? `55${digits}` : digits;
}

const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;

/** Linha da secretária (só com o modo ligado): com quem está falando e quantas respondeu hoje. null fora do modo. */
export function secretaryLine(data = {}) {
  if (!data.enabled || data.mode !== 'secretary') return null;
  const people = Number(data.contacts?.count) || (Array.isArray(data.contacts?.items) ? data.contacts.items.length : 0);
  if (!people) return { text: 'Ninguém em Contatos da Kira: ela não vai responder ninguém.', idle: true };
  const info = data.secretary || {};
  const replies = Number(info.replies_today) || 0;
  if (info.talking_to) return { text: `Conversando com ${info.talking_to} · ${plural(replies, 'resposta', 'respostas')} hoje`, idle: false };
  if (replies && info.last_contact) {
    const at = new Date(String(info.last_at || ''));
    const hour = Number.isNaN(at.getTime()) ? '' : ` às ${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`;
    return { text: `${plural(replies, 'resposta', 'respostas')} hoje · a última para ${info.last_contact}${hour}`, idle: false };
  }
  return { text: 'Ligada: esperando alguém da lista escrever.', idle: true };
}

export function createWhatsappTab({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  const doc = documentRef;
  const $ = (id) => doc.getElementById(id);
  let token = '';
  let timer = 0;

  const post = async (path, body = null) => {
    const response = await windowRef.fetch(path, body
      ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), cache: 'no-store' }
      : { cache: 'no-store' });
    return response.json();
  };
  const call = async (path, body = null) => {
    const payload = await post(path, body);
    return payload?.data ?? payload;
  };
  const say = (id, text) => { const node = $(id); if (node) node.textContent = text; };
  const status = (text) => say('wa-quick-status', text);
  const contactStatus = (text) => say('wa-contact-status', text);
  const pickContact = (name) => {
    const select = $('wa-quick-to');
    if (select) select.value = name;
    $('wa-quick-text')?.focus?.();
  };

  function fillPeople(names) {
    const select = $('wa-quick-to');
    if (!select) return;
    const current = select.value;
    const options = [el(doc, 'option', '', 'Para quem?')];
    options[0].value = '';
    for (const name of names) {
      const option = el(doc, 'option', '', name);
      option.value = name;
      options.push(option);
    }
    select.replaceChildren(...options);
    if (names.includes(current)) select.value = current;
  }

  async function removeContact(person) {
    contactStatus('Removendo…');
    try {
      const result = await post('/api/whatsapp/contacts/delete', { id: person.id });
      contactStatus(result?.success ? `${person.name} saiu da lista.` : (result?.error || 'Não consegui remover agora.'));
    } catch (_) {
      contactStatus('Não consegui remover agora.');
    }
    await refresh();
  }

  // Remover pede um segundo clique ("Remover?") para não tirar alguém sem querer.
  function askRemove(button, person) {
    if (!button.classList.contains('is-confirming')) {
      button.classList.add('is-confirming');
      button.textContent = 'Remover?';
      windowRef.setTimeout(() => {
        button.classList.remove('is-confirming');
        button.textContent = '✕';
      }, CONFIRM_REMOVE_MS);
      return;
    }
    removeContact(person);
  }

  function renderContacts(items) {
    const list = $('wa-contact-list');
    if (!list) return;
    const rows = items.map((person) => {
      const row = el(doc, 'div', 'wa-contact-row');
      row.setAttribute('role', 'button');
      row.tabIndex = 0;
      row.title = `Escrever para ${person.name}`;
      const text = el(doc, 'span', 'wa-contact-text');
      text.append(el(doc, 'b', '', person.name), el(doc, 'span', '', [person.saved && person.saved !== person.name ? `salvo como ${person.saved}` : '', person.number].filter(Boolean).join(' · ') || 'sem número'));
      const remove = el(doc, 'button', 'wa-contact-remove', '✕');
      remove.type = 'button';
      remove.title = `Remover ${person.name}`;
      remove.setAttribute('aria-label', `Remover ${person.name}`);
      remove.addEventListener('click', (event) => {
        event?.stopPropagation?.();
        askRemove(remove, person);
      });
      const mood = el(doc, 'select', 'wa-contact-mood');
      mood.title = `Como a Kira trata ${person.name}`;
      mood.dataset.number = person.number || '';
      mood.addEventListener('click', (event) => event?.stopPropagation?.());
      mood.addEventListener('keydown', (event) => event?.stopPropagation?.());
      mood.addEventListener('change', () => {
        windowRef.fetch('/api/whatsapp/mood', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ number: person.number || '', preset: mood.value }) })
          .then(() => loadMoods()).catch(() => {});
      });
      if (!person.number) mood.disabled = true;
      row.append(el(doc, 'span', 'wa-contact-avatar', initial(person.name)), text, mood, remove);
      row.addEventListener('click', () => pickContact(person.name));
      row.addEventListener('keydown', (event) => { if (event?.key === 'Enter') pickContact(person.name); });
      return row;
    });
    if (!rows.length) rows.push(el(doc, 'div', 'kira-settings-help', 'Nenhum contato ainda: adicione abaixo.'));
    list.replaceChildren(...rows);
    loadMoods();
  }

  // Personalidade por contato: "Automático" segue como o contato trata a Kira (sem nota = a mesma das Configurações).
  function loadMoods() {
    windowRef.fetch('/api/whatsapp/moods', { cache: 'no-store' }).then((r) => r.json()).then((data) => {
      const presets = data?.presets || {};
      const moods = data?.moods || {};
      const keyOf = (n) => { const d = String(n || '').replace(/\D/g, ''); return d.length >= 8 ? d.slice(-8) : ''; };
      for (const select of $('wa-contact-list')?.querySelectorAll?.('select.wa-contact-mood') || []) {
        const m = moods[keyOf(select.dataset.number)] || {};
        const autoLabel = m.auto ? presets[m.auto] : 'igual à sua';
        const opts = [['auto', `Automático (${autoLabel})`], ...Object.entries(presets)];
        select.replaceChildren(...opts.map(([v, label]) => { const o = el(doc, 'option', '', label); o.value = v; return o; }));
        select.value = m.manual || 'auto';
      }
    }).catch(() => {});
  }

  function render(data = {}) {
    const off = !data.enabled;
    for (const [id, value] of [['wa-today-received', data.today?.received], ['wa-today-sent', data.today?.sent], ['wa-today-chats', data.today?.chats]]) {
      say(id, off ? '–' : String(Number(value) || 0));
    }
    const people = (Array.isArray(data.contacts?.items) ? data.contacts.items : [])
      .filter((person) => person && person.name)
      .map((person) => ({ id: String(person.id || ''), name: String(person.name), number: String(person.number || ''), saved: String(person.whatsapp_name || '') }));
    renderContacts(people);
    say('wa-contact-count', String(Number(data.contacts?.count) || people.length));
    const line = secretaryLine(data);
    const secretary = $('wa-secretary-status');
    if (secretary) {
      secretary.hidden = !line;
      secretary.textContent = line?.text || '';
      secretary.classList.toggle('is-idle', Boolean(line?.idle));
    }
    // "Para quem?": contatos da Kira e quem falou com você hoje
    const recent = Array.isArray(data.recent) ? data.recent.map((item) => String(item?.contact || '')) : [];
    fillPeople([...new Set([...people.map((person) => person.name), ...recent].filter(Boolean))]);
  }

  async function refresh() {
    try {
      render(await call('/api/whatsapp/overview'));
    } catch (_) {
      /* aba continua com o último resumo */
    }
  }

  async function addContact(event) {
    event?.preventDefault?.();
    const name = String($('wa-contact-name')?.value || '').trim();
    const saved = String($('wa-contact-saved')?.value || '').trim(); // nome na agenda do celular: é o que a Kira pesquisa
    const number = contactDigits($('wa-contact-number')?.value);
    if (!name || (!saved && number.length < 11) || number.length > 15 || (number && number.length < 11)) {
      contactStatus('Escreva o nome e o nome salvo no celular ou o número com DDD.');
      return;
    }
    contactStatus('Salvando…');
    try {
      const result = await post('/api/whatsapp/contacts', { name, number, whatsapp_name: saved || name });
      if (!result?.success) {
        contactStatus(result?.error || 'Não consegui salvar agora.');
        return;
      }
      for (const id of ['wa-contact-name', 'wa-contact-saved', 'wa-contact-number']) { const input = $(id); if (input) input.value = ''; }
      contactStatus(`${name} entrou na lista.`);
      const form = $('wa-contact-add'); if (form) form.hidden = true; // fecha a telinha do ⋮
      await refresh();
    } catch (_) {
      contactStatus('Não consegui salvar agora.');
    }
  }

  function showConfirm(show, text = '') {
    const box = $('wa-quick-confirm');
    if (box) box.hidden = !show;
    say('wa-quick-preview', text);
  }

  async function prepare() {
    const contact = String($('wa-quick-to')?.value || '').trim();
    const message = String($('wa-quick-text')?.value || '').trim();
    if (!contact || !message) {
      status(!contact ? 'Escolha para quem.' : 'Escreva a mensagem.');
      return;
    }
    status('Preparando…');
    try {
      // verbatim: sai do jeito que foi digitado, no nome do Chefe (sem a pergunta de estilo dos pedidos por voz)
      const result = await call('/api/whatsapp/prepare-message', { contact, message, raw_text: message, verbatim: true });
      if (result?.confirm_token) {
        token = result.confirm_token;
        status('');
        showConfirm(true, `Enviar para ${result.contact || contact}: “${result.message || message}”?`);
        return;
      }
      status(result?.clarification_question || (result?.error === 'contact_not_found' ? 'Não achei esse contato.' : 'Não consegui preparar agora.'));
    } catch (_) {
      status('Não consegui preparar agora.');
    }
  }

  async function confirm() {
    if (!token) return;
    const sending = token;
    token = '';
    showConfirm(false);
    status('Enviando…');
    try {
      const result = await call('/api/whatsapp/confirm-send', { confirm_token: sending });
      status(result?.ok ? 'Enviado.' : 'Não foi enviado.');
      if (result?.ok) {
        const text = $('wa-quick-text');
        if (text) text.value = '';
        refresh();
      }
    } catch (_) {
      status('Não foi enviado.');
    }
  }

  async function cancel() {
    const canceled = token;
    token = '';
    showConfirm(false);
    status('Cancelado.');
    if (canceled) await call('/api/whatsapp/cancel-send', { confirm_token: canceled }).catch(() => null);
  }

  const replies = $('wa-quick-replies');
  if (replies) {
    replies.replaceChildren(...QUICK_REPLIES.map((reply) => {
      const chip = el(doc, 'button', 'kira-settings-chip', reply);
      chip.type = 'button';
      chip.addEventListener('click', () => { const text = $('wa-quick-text'); if (text) text.value = reply; });
      return chip;
    }));
  }
  $('wa-quick-prepare')?.addEventListener('click', prepare);
  $('wa-quick-confirm-btn')?.addEventListener('click', confirm);
  $('wa-quick-cancel-btn')?.addEventListener('click', cancel);
  $('wa-contact-add')?.addEventListener('submit', addContact);

  // Só consulta com a aba aberta (e a janela à mostra), a cada 15 s.
  const drawer = $('whatsapp-drawer');
  const sync = () => {
    const open = Boolean(drawer?.classList?.contains('open'));
    if (open && !timer) {
      refresh();
      timer = windowRef.setInterval(() => { if (!doc.hidden) refresh(); }, REFRESH_MS);
    } else if (!open && timer) {
      windowRef.clearInterval(timer);
      timer = 0;
    }
  };
  if (drawer && windowRef.MutationObserver) new windowRef.MutationObserver(sync).observe(drawer, { attributes: true, attributeFilter: ['class'] });
  sync();
  return { refresh, render, prepare, confirm, cancel, addContact };
}

if (globalThis.document?.getElementById?.('whatsapp-drawer') && globalThis.window && !globalThis.window.KiraWhatsappTab) {
  globalThis.window.KiraWhatsappTab = createWhatsappTab();
}
