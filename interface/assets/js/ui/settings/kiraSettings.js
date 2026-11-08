// Aba "Kira" das configurações: cidade, recursos (agenda, e-mail, saúde, WhatsApp...), avisos por voz,
// horário de silêncio, briefing, interesses e o estado do sistema. Tudo montado com createElement/textContent.
// Monta uma vez; ao voltar para a aba só redesenha se algo mudou (antes refazia tudo e animava de novo a cada clique).

const TOPICS = [['geral', 'Geral'], ['tecnologia', 'Tecnologia'], ['games', 'Games'], ['esportes', 'Esportes'], ['ciencia', 'Ciência'], ['economia', 'Economia']];
const SECTIONS = [['clima', 'Clima'], ['agenda', 'Agenda'], ['noticias', 'Notícias'], ['cotacoes', 'Cotações'], ['historia', 'Hoje na história'], ['lembretes', 'Lembretes']];
const NEWS_COUNTS = [[3, '3'], [4, '4'], [6, '6']];
// Configurações > Personalidade (services/kira_settings.py valida 0-100 e monta as regras para o Gemini)
const TRAIT_GROUPS = [
  ['Comportamento e temperamento', [['grosseria', 'Grosseria'], ['brutalidade', 'Brutalidade'], ['ignorancia_teatral', 'Ignorância teatral'], ['impaciencia', 'Impaciência'], ['teimosia', 'Teimosia'], ['provocacoes', 'Provocações'], ['deboche', 'Deboche']]],
  ['Humor', [['sarcasmo', 'Sarcasmo'], ['ironia', 'Ironia'], ['humor_acido', 'Humor ácido'], ['palavroes', 'Palavrões'], ['espontaneidade', 'Espontaneidade']]],
  ['Afetividade', [['carinho', 'Carinho'], ['meiguice', 'Meiguice'], ['sensibilidade', 'Sensibilidade'], ['ciumes_simulados', 'Ciúmes simulados'], ['elogios', 'Elogios'], ['bajulacao', 'Bajulação'], ['intimidade', 'Intimidade']]],
  ['Comunicação', [['formalidade', 'Formalidade'], ['paciencia', 'Paciência'], ['proatividade', 'Proatividade'], ['intensidade_intervencoes', 'Intensidade das intervenções']]],
];
const TRAITS = TRAIT_GROUPS.flatMap(([, traits]) => traits);
/* Mantém uma lista única de controles: os grupos só organizam a apresentação. */
/* eslint-disable-next-line no-unused-vars */
const LEGACY_TRAIT_LABELS = [['grosseria', 'Grosseria'], ['brutalidade', 'Brutalidade'], ['ignorancia_teatral', 'Ignorância teatral'], ['sarcasmo', 'Sarcasmo'],
  ['ironia', 'Ironia'], ['humor_acido', 'Humor ácido'], ['palavroes', 'Palavrões'], ['deboche', 'Deboche'], ['provocacoes', 'Provocações'],
  ['carinho', 'Carinho'], ['meiguice', 'Meiguice'], ['sensibilidade', 'Sensibilidade'], ['ciumes_simulados', 'Ciúmes simulados'],
  ['elogios', 'Elogios'], ['bajulacao', 'Bajulação'], ['paciencia', 'Paciência'], ['formalidade', 'Formalidade'], ['intimidade', 'Intimidade'],
  ['espontaneidade', 'Espontaneidade'], ['proatividade', 'Proatividade'], ['teimosia', 'Teimosia']];
const PRESETS = [['padrao', 'Padrão'], ['bruta', 'Bruta'], ['carinhosa', 'Carinhosa'], ['sarcástica', 'Sarcástica'], ['profissional', 'Profissional'], ['personalizado', 'Personalizada']];

const NAMED_KEYS = { Space: 'Space', Insert: 'Insert', Home: 'Home', End: 'End', PageUp: 'PageUp', PageDown: 'PageDown', Pause: 'Pause', ScrollLock: 'ScrollLock', Backquote: 'Backquote' };
const SHORTCUT_STATUS = { ok: 'Ativo', em_uso: 'Em uso por outro programa: escolha outra combinação', invalido: 'Combinação inválida', desligado: 'Desligado' };

/** Combinação pressionada -> "Ctrl+Shift+S" (o backend registra no Windows); null enquanto só há modificadores. */
export function comboFromEvent(event) {
  const code = String(event?.code || '');
  let key = '';
  if (/^Key[A-Z]$/.test(code)) key = code.slice(3);
  else if (/^Digit[0-9]$/.test(code)) key = code.slice(5);
  else if (/^F([1-9]|1[0-9]|2[0-4])$/.test(code)) key = code;
  else if (NAMED_KEYS[code]) key = NAMED_KEYS[code];
  if (!key) return null;
  const mods = [event.ctrlKey && 'Ctrl', event.altKey && 'Alt', event.shiftKey && 'Shift', event.metaKey && 'Win'].filter(Boolean);
  if (!mods.length && !key.startsWith('F')) return null; // tecla sozinha atrapalharia a digitação
  return [...mods, key].join('+');
}

// Ícones dos cartões (traço simples, 24x24).
const ICONS = {
  sparkles: 'M12 3l1.8 4.6L18 9.5l-4.2 1.9L12 16l-1.8-4.6L6 9.5l4.2-1.9Z M19 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8Z',
  pin: 'M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21Z M12 12a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z',
  grid: 'M4 4h7v7H4z M13 4h7v7h-7z M4 13h7v7H4z M13 13h7v7h-7z',
  bell: 'M6 16V11a6 6 0 1 1 12 0v5l2 2H4l2-2Z M10 20a2 2 0 0 0 4 0',
  keyboard: 'M3 6h18v12H3z M7 10h.01 M11 10h.01 M15 10h.01 M7 14h10',
  news: 'M4 5h13v14H6a2 2 0 0 1-2-2V5Z M17 9h3v8a2 2 0 0 1-2 2 M8 9h5 M8 13h5',
  star: 'M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9L12 3Z',
  pulse: 'M3 12h4l2-6 4 12 2-6h6',
  send: 'M4 12 20 4l-6 16-3-7-7-1Z M11 13l9-9',
};

// Exemplos de endereço do Apprise (o endereço tem chave: vai para o cofre do backend e não volta para a tela).
const NOTIFY_EXAMPLES = 'Celular (mais fácil): instale o app ntfy, assine um tópico difícil de adivinhar e cole ntfy://seu-topico. '
  + 'Telegram: tgram://TOKEN_DO_BOT/ID_DO_CHAT. Discord: o link do webhook do canal. '
  + 'E-mail: mailtos://usuario:senha-de-app@gmail.com. WhatsApp: whatsapp://TOKEN@ID_DO_NUMERO/SEU_NUMERO.';

function svgIcon(doc, name) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = doc.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'kira-settings-icon');
  svg.setAttribute('aria-hidden', 'true');
  const path = doc.createElementNS(NS, 'path');
  path.setAttribute('d', ICONS[name] || '');
  svg.append(path);
  return svg;
}

function el(doc, tag, className = '', text = '') {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

async function api(win, method, path, body) {
  const response = await win.fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store',
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

/** /api/system-status -> linhas [nome, valor, ok] para o cartão Sistema (só rótulos; nada de chave ou e-mail). */
export function systemLines(status = {}) {
  const good = new Set(['online', 'connected', 'ready', 'excellent', 'good']);
  const rows = [
    ['Saúde', status?.health],
    ['Voz', status?.voice],
    ['Cérebro (Gemini)', status?.api],
    ['LiveKit', status?.livekit],
    ['WhatsApp', status?.whatsapp],
  ];
  return rows
    .filter(([, item]) => item && typeof item === 'object' && item.label)
    .map(([name, item]) => [name, String(item.label).slice(0, 40), good.has(String(item.status || item.level || '').toLowerCase())]);
}

export class KiraSettingsTab {
  constructor({ root, personalityRoot = null, window: win = globalThis.window } = {}) {
    this.root = root;
    this.personalityRoot = personalityRoot;
    this.window = win;
    this.document = root?.ownerDocument || win?.document;
    this.state = null;
  }

  async load() {
    const [settings, interests, google, voices, system, notify] = await Promise.all([
      api(this.window, 'GET', '/api/ui/settings'),
      api(this.window, 'GET', '/api/ui/interests').catch(() => ({ topics: [] })),
      api(this.window, 'GET', '/api/ui/google/status').catch(() => ({})),
      api(this.window, 'GET', '/api/ui/voice-profile').catch(() => ({ profiles: [] })),
      api(this.window, 'GET', '/api/system-status').catch(() => ({})),
      api(this.window, 'GET', '/api/notify/destinations').catch(() => ({ destinations: [] })),
    ]);
    const signature = JSON.stringify([settings, interests, google, voices, systemLines(system), notify?.destinations]);
    if (signature === this.signature) return; // nada mudou: a aba aparece na hora, sem redesenhar
    const first = !this.signature;
    this.signature = signature;
    this.state = { ...settings.settings, city: settings.city || '' };
    this.shortcutStatus = settings.shortcutStatus || {};
    this.interests = (interests.topics || []).map((t) => String(t.term || t)).filter(Boolean);
    this.google = google || {};
    this.voices = (voices.profiles || []).map((v) => String(v.name || '')).filter(Boolean);
    this.system = systemLines(system);
    this.destinations = Array.isArray(notify?.destinations) ? notify.destinations : [];
    this.render(first);
  }

  async save(patch, note = 'Salvo.') {
    try {
      const result = await api(this.window, 'PUT', '/api/ui/settings', patch);
      this.state = { ...this.state, ...result.settings };
      this.flash(note);
    } catch (error) {
      this.flash('Não consegui salvar agora.');
    }
  }

  flash(text) {
    if (this.status) this.status.textContent = text;
  }

  card(title, subtitle, accent, icon) {
    const doc = this.document;
    const card = el(doc, 'section', 'kira-settings-card');
    card.style.setProperty('--card-accent', accent);
    card.style.setProperty('--i', String(this.cardCount = (this.cardCount || 0) + 1));
    const head = el(doc, 'header', 'kira-settings-head');
    head.append(svgIcon(doc, icon), el(doc, 'h3', 'kira-settings-title', title));
    if (subtitle) head.append(el(doc, 'p', 'kira-settings-sub', subtitle));
    const body = el(doc, 'div', 'kira-settings-body');
    card.append(head, body);
    return { card, body };
  }

  toggleRow(label, help, value, onChange) {
    const doc = this.document;
    const row = el(doc, 'div', 'kira-settings-row');
    const text = el(doc, 'div', 'kira-settings-row-text');
    text.append(el(doc, 'div', 'kira-settings-label', label));
    if (help) text.append(el(doc, 'div', 'kira-settings-help', help));
    const button = el(doc, 'button', `kira-switch${value ? ' enabled' : ''}`);
    button.type = 'button';
    button.setAttribute('role', 'switch');
    button.setAttribute('aria-label', label);
    button.setAttribute('aria-checked', value ? 'true' : 'false');
    button.addEventListener('click', () => {
      const next = !button.classList.contains('enabled');
      button.classList.toggle('enabled', next);
      button.setAttribute('aria-checked', next ? 'true' : 'false');
      onChange(next);
    });
    row.append(text, button);
    return row;
  }

  chips(options, selected, onChange) {
    const doc = this.document;
    const wrap = el(doc, 'div', 'kira-settings-chips');
    for (const [value, label] of options) {
      const chip = el(doc, 'button', `kira-settings-chip${selected.includes(value) ? ' active' : ''}`, label);
      chip.type = 'button';
      chip.setAttribute('aria-pressed', selected.includes(value) ? 'true' : 'false');
      chip.addEventListener('click', () => {
        chip.setAttribute('aria-pressed', chip.classList.toggle('active') ? 'true' : 'false');
        onChange([...wrap.querySelectorAll('.kira-settings-chip.active')].map((c) => c.dataset.value));
      });
      chip.dataset.value = value;
      wrap.append(chip);
    }
    return wrap;
  }

  /** Uma escolha só entre opções (ex.: quantas notícias). */
  choice(options, selected, onChange) {
    const doc = this.document;
    const wrap = el(doc, 'div', 'kira-settings-chips');
    wrap.setAttribute('role', 'radiogroup');
    const chips = options.map(([value, label]) => {
      const chip = el(doc, 'button', `kira-settings-chip${value === selected ? ' active' : ''}`, label);
      chip.type = 'button';
      chip.setAttribute('role', 'radio');
      chip.setAttribute('aria-checked', value === selected ? 'true' : 'false');
      chip.addEventListener('click', () => {
        for (const other of chips) {
          other.classList.toggle('active', other === chip);
          other.setAttribute('aria-checked', other === chip ? 'true' : 'false');
        }
        onChange(value);
      });
      wrap.append(chip);
      return chip;
    });
    return wrap;
  }

  /** "Como conectar Agenda e Gmail": passo a passo (services/setup_help.py, o mesmo que a Kira fala) e a credencial. */
  googleHelp() {
    const doc = this.document;
    const box = el(doc, 'details', 'kira-settings-help-box');
    box.append(el(doc, 'summary', '', 'Como conectar Agenda e Gmail (passo a passo)'));
    const body = el(doc, 'div', 'kira-settings-help-body');
    box.append(body);
    box.addEventListener('toggle', async () => {
      if (!box.open || body.childElementCount) return;
      let help = {};
      try { help = await api(this.window, 'GET', '/api/ui/google/help'); } catch (_) { /* sem rede: só o formulário */ }
      body.append(el(doc, 'p', 'kira-settings-help', help.resumo || ''));
      const list = el(doc, 'ol', 'kira-settings-steps');
      (help.passos || []).forEach((step) => list.append(el(doc, 'li', '', step)));
      body.append(list, el(doc, 'p', 'kira-settings-help', help.celular || ''));
      (help.problemas || []).forEach((item) => body.append(el(doc, 'p', 'kira-settings-help', `Se der erro: ${item}`)));
      body.append(el(doc, 'b', '', help.has_app ? 'Credencial do Google: salva ✓' : 'Credencial do Google (passo 7)'));
      const id = el(doc, 'input', 'kira-settings-field');
      id.placeholder = 'ID do cliente (…apps.googleusercontent.com)';
      const secret = el(doc, 'input', 'kira-settings-field');
      secret.type = 'password';
      secret.placeholder = 'Chave secreta do cliente';
      const row = el(doc, 'div', 'kira-settings-inline');
      row.append(id, secret, this.button('Salvar', async () => {
        try {
          const result = await api(this.window, 'POST', '/api/ui/google/app', { client_id: id.value, client_secret: secret.value });
          this.flash(result.message || (result.ok ? 'Salvo.' : 'Não salvei.'));
          if (result.ok) { id.value = ''; secret.value = ''; }
        } catch (_) { this.flash('Não salvei.'); }
      }));
      body.append(row);
    });
    return box;
  }

  button(label, onClick, className = 'kira-settings-button') {
    const button = el(this.document, 'button', className, label);
    button.type = 'button';
    button.addEventListener('click', onClick);
    return button;
  }

  locationBox() {
    const doc = this.document;
    const box = el(doc, 'form', 'kira-settings-location');
    box.setAttribute('aria-label', 'Cidade');
    const city = el(doc, 'input', 'kira-settings-location-input');
    city.value = this.state.city || '';
    city.placeholder = 'Sua cidade';
    city.maxLength = 80;
    city.title = 'Cidade usada no clima e no briefing quando você não disser outra.';
    city.setAttribute('aria-label', 'Cidade do clima e do briefing');
    const save = el(doc, 'button', 'kira-settings-location-save', 'OK');
    save.type = 'submit';
    box.addEventListener('submit', async (event) => {
      event.preventDefault();
      const value = city.value.trim();
      try {
        await api(this.window, 'PUT', '/api/ui/location', { city: value });
        this.flash(value ? `Cidade: ${value}.` : 'Cidade removida.');
      } catch (error) {
        this.flash('Não consegui salvar a cidade.');
      }
    });
    box.append(svgIcon(doc, 'pin'), city, save);
    return box;
  }

  pill(label, value, ok = true) {
    const doc = this.document;
    const pill = el(doc, 'span', 'kira-settings-pill');
    const dot = el(doc, 'span', `kira-settings-summary-dot${ok ? '' : ' is-off'}`);
    pill.append(dot, el(doc, 'span', '', `${label} `), el(doc, 'b', '', value));
    return pill;
  }

  render(first = false) {
    const doc = this.document;
    const s = this.state;
    this.cardCount = 0;
    this.root.replaceChildren();
    // Só a primeira montagem anima os cartões.
    this.root.classList.toggle('is-first-render', first);
    if (first) this.window.setTimeout?.(() => this.root.classList.remove('is-first-render'), 1200);

    const top = el(doc, 'div', 'kira-settings-top');
    const on = [s.agenda_enabled, s.email_enabled, s.health_enabled, s.modules?.music, s.modules?.vision].filter(Boolean).length;
    const pills = el(doc, 'div', 'kira-settings-pills');
    pills.append(
      this.pill('Recursos', `${on}/5`),
      this.pill('Voz', this.voices.length ? this.voices.join(', ') : 'ninguém', this.voices.length > 0),
      this.pill('Interesses', String(this.interests.length)),
    );
    top.append(this.locationBox(), pills);

    const features = this.card('Recursos', 'Ligue só o que você usa.', '#22d3ee', 'grid');
    // Ajudas de uma linha (com duas, a aba não cabia sem rolar); a conexão Google aparece nos botões abaixo.
    features.body.append(
      this.toggleRow('Agenda', 'Google Agenda, só leitura.', s.agenda_enabled, (v) => this.save({ agenda_enabled: v })),
      this.toggleRow('E-mail', 'Gmail recente, só leitura.', s.email_enabled, (v) => this.save({ email_enabled: v })),
      this.toggleRow('Saúde', 'Dados que você importou.', s.health_enabled, (v) => this.save({ health_enabled: v })),
      this.toggleRow('Música', 'Player integrado (ao reabrir).', s.modules?.music, (v) => this.save({ modules: { music: v } }, 'Salvo. Vale ao reabrir a Kira.')),
      this.toggleRow('Visão de tela', 'Olhar a tela (ao reabrir).', s.modules?.vision, (v) => this.save({ modules: { vision: v } }, 'Salvo. Vale ao reabrir a Kira.')),
    );
    const connects = el(doc, 'div', 'kira-settings-inline');
    for (const [service, label] of [['calendar', 'Conectar Agenda'], ['email', 'Conectar Gmail']]) {
      if (this.google[service]) continue;
      connects.append(this.button(label, async () => {
        try {
          const result = await api(this.window, 'POST', '/api/ui/google/connect', { service });
          this.flash(result.ok ? 'Abra o navegador para autorizar.' : (result.message || 'Não foi possível conectar.'));
        } catch (error) {
          this.flash('Não foi possível conectar.');
        }
      }));
    }
    if (connects.childElementCount) features.body.append(connects);
    features.body.append(this.googleHelp());

    const voice = this.card('Avisos por voz', 'Lembretes e alarmes falados.', '#fbbf24', 'bell');
    const quietRow = this.toggleRow('Horário de silêncio', 'Os avisos esperam e falam depois.', s.quiet_hours?.enabled, (v) => this.save({ quiet_hours: { enabled: v } }));
    const quiet = el(doc, 'div', 'kira-settings-inline kira-settings-quiet');
    for (const edge of ['start', 'end']) {
      const input = el(doc, 'input', 'kira-settings-field kira-settings-time');
      input.type = 'time';
      input.value = s.quiet_hours?.[edge] || (edge === 'start' ? '22:00' : '07:00');
      input.setAttribute('aria-label', edge === 'start' ? 'Início do silêncio' : 'Fim do silêncio');
      input.addEventListener('change', () => this.save({ quiet_hours: { [edge]: input.value } }));
      quiet.append(el(doc, 'span', 'kira-settings-help', edge === 'start' ? 'das' : 'até'), input);
    }
    voice.body.append(
      this.toggleRow('Falar lembretes', 'Sem isso, só o sininho.', s.reminders_voice, (v) => this.save({ reminders_voice: v })),
      this.toggleRow('Proatividade', 'Oferece lembrete de compromissos.', s.proactive_offers, (v) => this.save({ proactive_offers: v })),
      quietRow,
      quiet,
    );

    const brief = this.card('Briefing e notícias', 'O que entra no "bom dia".', '#34d399', 'news');
    const countRow = el(doc, 'div', 'kira-settings-between');
    countRow.append(
      el(doc, 'div', 'kira-settings-label', 'Quantas notícias'),
      this.choice(NEWS_COUNTS, Number(s.briefing_news_count) || 4, (count) => this.save({ briefing_news_count: count })),
    );
    brief.body.append(
      el(doc, 'div', 'kira-settings-label', 'Seções do briefing'),
      this.chips(SECTIONS, SECTIONS.map(([v]) => v).filter((v) => s.briefing_sections?.[v] !== false), (active) => {
        this.save({ briefing_sections: Object.fromEntries(SECTIONS.map(([v]) => [v, active.includes(v)])) });
      }),
      countRow,
      el(doc, 'div', 'kira-settings-label', 'Temas das notícias'),
      this.chips(TOPICS, s.news_topics || [], (active) => this.save({ news_topics: active })),
    );

    const likes = this.card('Interesses', 'Sobem nas notícias (ex.: Nvidia).', '#f472b6', 'star');
    const list = el(doc, 'div', 'kira-settings-chips');
    const draw = () => {
      list.replaceChildren();
      for (const term of this.interests) {
        const chip = el(doc, 'button', 'kira-settings-chip active', `${term} ×`);
        chip.type = 'button';
        chip.setAttribute('aria-label', `Remover ${term}`);
        chip.addEventListener('click', () => this.saveInterests(this.interests.filter((t) => t !== term), draw));
        list.append(chip);
      }
    };
    draw();
    const addRow = el(doc, 'form', 'kira-settings-inline');
    const term = el(doc, 'input', 'kira-settings-field kira-settings-grow');
    term.placeholder = 'Novo interesse';
    term.maxLength = 40;
    term.setAttribute('aria-label', 'Novo interesse');
    const add = el(doc, 'button', 'kira-settings-button', 'Adicionar');
    add.type = 'submit';
    addRow.addEventListener('submit', (event) => {
      event.preventDefault();
      const value = term.value.trim();
      if (!value) return;
      term.value = '';
      this.saveInterests([...new Set([...this.interests, value])], draw);
    });
    addRow.append(term, add);
    likes.body.append(list, addRow);

    // Três colunas fixas (antes o navegador distribuía e o cartão Sistema ia parar lá embaixo, fora da vista).
    const grid = el(doc, 'div', 'kira-settings-grid');
    const personality = this.personalityCard();
    const columns = [[features.card, likes.card], [this.shortcutsCard(), voice.card, this.notifyCard()], [this.systemCard(), brief.card]];
    for (const cards of columns) {
      const column = el(doc, 'div', 'kira-settings-col');
      column.append(...cards);
      grid.append(column);
    }
    this.status = el(doc, 'div', 'kira-settings-status', 'As alterações são aplicadas automaticamente.');
    this.status.setAttribute('aria-live', 'polite');
    const scroller = el(doc, 'div', 'kira-settings-scroll');
    scroller.append(grid);
    this.root.append(top, scroller, this.status);
    if (this.personalityRoot) {
      this.personalityRoot.replaceChildren(personality);
    }
  }

  /** Avisos para fora: lista de destinos (nome e tipo), testar, remover e cadastrar um novo. */
  personalityCard() {
    const doc = this.document;
    const p = this.state.personality || {};
    const { card, body } = this.card('Personalidade', 'Aplicada imediatamente às respostas reais. Só muda pelos seus controles.', '#f472b6', 'sparkles');
    const sliders = el(doc, 'div', 'kira-persona-sliders');
    const draw = (values) => sliders.replaceChildren(...TRAIT_GROUPS.map(([group, traits]) => {
      const section = el(doc, 'section', 'kira-persona-group');
      section.append(el(doc, 'h4', 'kira-persona-group-title', group));
      section.append(...traits.map(([key, label]) => {
      const row = el(doc, 'label', 'kira-persona-row');
      const input = el(doc, 'input');
      Object.assign(input, { type: 'range', min: 0, max: 100, step: 5, value: values[key] ?? 50 });
      const out = el(doc, 'b', '', String(input.value));
      input.addEventListener('input', () => { out.textContent = input.value; });
      input.addEventListener('change', () => this.save({ personality: { [key]: Number(input.value) } }));
      row.append(el(doc, 'span', '', label), input, out);
      return row;
      }));
      return section;
    }));
    const preset = async (value) => { await this.save({ personality: value === 'reset' ? { reset: true } : { preset: value } }); draw(this.state.personality || {}); };
    const names = (key, placeholder) => {
      const input = el(doc, 'input', 'kira-settings-field');
      Object.assign(input, { value: p[key] || '', placeholder, maxLength: 60 });
      input.addEventListener('change', () => this.save({ personality: { [key]: input.value } }));
      return input;
    };
    const reset = el(doc, 'button', 'kira-settings-chip', 'Voltar ao padrão');
    reset.type = 'button';
    reset.addEventListener('click', () => preset('reset'));
    body.append(this.choice(PRESETS, p.preset, preset), names('apelido_dono', 'Como ela te chama (ex.: chefe)'), names('apelidos', 'Outros apelidos'), sliders, reset);
    draw(p);
    return card;
  }

  notifyCard() {
    const doc = this.document;
    const card = this.card('Avisos', 'Para onde a Kira manda recados.', '#60a5fa', 'send');
    const list = el(doc, 'div');
    const draw = () => {
      list.replaceChildren();
      for (const item of this.destinations) {
        const row = el(doc, 'div', 'kira-settings-between');
        const text = el(doc, 'div', 'kira-settings-row-text');
        text.append(el(doc, 'div', 'kira-settings-label', String(item.name || '')), el(doc, 'div', 'kira-settings-help', String(item.kind || '')));
        const actions = el(doc, 'div', 'kira-settings-inline kira-settings-nowrap');
        const test = this.button('Testar', async () => {
          test.disabled = true;
          try {
            const result = await api(this.window, 'POST', `/api/notify/test/${encodeURIComponent(item.id)}`);
            this.flash(result.ok ? `Teste enviado para ${item.name}.` : `${item.name} não respondeu. Confira o endereço.`);
          } catch (error) {
            this.flash('Não consegui testar agora.');
          }
          test.disabled = false;
        });
        const remove = this.button('×', async () => {
          try {
            const result = await api(this.window, 'DELETE', `/api/notify/destinations/${encodeURIComponent(item.id)}`);
            this.destinations = result.destinations || [];
            draw();
            this.flash('Destino removido.');
          } catch (error) {
            this.flash('Não consegui remover agora.');
          }
        }, 'kira-settings-button kira-settings-icon-button');
        remove.setAttribute('aria-label', `Remover ${item.name}`);
        actions.append(test, remove);
        row.append(text, actions);
        list.append(row);
      }
      if (!this.destinations.length) list.append(el(doc, 'div', 'kira-settings-help', 'Nenhum destino ainda.'));
    };
    draw();
    const form = el(doc, 'form', 'kira-settings-inline');
    const name = el(doc, 'input', 'kira-settings-field');
    name.placeholder = 'Nome (ex.: Celular)';
    name.maxLength = 40;
    name.setAttribute('aria-label', 'Nome do destino');
    const url = el(doc, 'input', 'kira-settings-field kira-settings-grow');
    url.placeholder = 'Endereço (ex.: ntfy://meu-topico)';
    url.maxLength = 1000;
    url.type = 'password';
    url.autocomplete = 'off';
    url.setAttribute('aria-label', 'Endereço do destino');
    const add = el(doc, 'button', 'kira-settings-button', 'Adicionar');
    add.type = 'submit';
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      try {
        const result = await api(this.window, 'POST', '/api/notify/destinations', { name: name.value, url: url.value });
        this.destinations = result.destinations || [];
        if (result.ok) {
          name.value = '';
          url.value = '';
          draw();
          this.flash('Destino salvo no cofre. Use Testar para conferir.');
        } else {
          this.flash(result.error || 'Não consegui salvar.');
        }
      } catch (error) {
        this.flash('Não consegui salvar (só na tela do PC).');
      }
    });
    form.append(name, url, add);
    card.body.append(list, form, el(doc, 'div', 'kira-settings-help', NOTIFY_EXAMPLES));
    return card.card;
  }

  /** Estado do sistema numa olhada: saúde, voz, cérebro (Gemini), LiveKit e WhatsApp. */
  systemCard() {
    const doc = this.document;
    const card = this.card('Sistema', 'Como a Kira está agora.', '#38bdf8', 'pulse');
    const list = el(doc, 'dl', 'kira-system-list');
    for (const [name, value, ok] of this.system) {
      const valueNode = el(doc, 'dd', ok ? 'is-ok' : 'is-warn', value);
      list.append(el(doc, 'dt', '', name), valueNode);
    }
    if (!this.system.length) list.append(el(doc, 'dt', '', 'Estado'), el(doc, 'dd', 'is-warn', 'indisponível agora'));
    card.body.append(list);
    return card.card;
  }

  shortcutsCard() {
    const doc = this.document;
    const card = this.card('Atalhos', 'Valem com a Kira minimizada.', '#a78bfa', 'keyboard');
    if (card.dataset) card.dataset.pcOnly = ''; // celular não tem atalhos de teclado
    const shortcuts = this.state.shortcuts || {};
    const rows = [
      ['screen_share', 'Tela', 'Liga/desliga a visão.'],
      ['mic', 'Microfone', 'Liga ou desliga.'],
    ];
    for (const [name, label, help] of rows) {
      const row = el(doc, 'div', 'kira-settings-row');
      const text = el(doc, 'div', 'kira-settings-row-text');
      const status = el(doc, 'div', 'kira-settings-help kira-shortcut-status', shortcuts[name] ? (SHORTCUT_STATUS[this.shortcutStatus[name]] || '') : '');
      text.append(el(doc, 'div', 'kira-settings-label', label), el(doc, 'div', 'kira-settings-help', help), status);
      const capture = el(doc, 'button', 'kira-settings-button kira-shortcut-capture', shortcuts[name] || 'Definir');
      capture.type = 'button';
      capture.setAttribute('aria-label', `Atalho: ${label}`);
      capture.addEventListener('click', () => this.captureShortcut(name, capture, status));
      const clear = this.button('×', () => this.saveShortcuts({ [name]: '' }, capture, status), 'kira-settings-button kira-settings-icon-button');
      clear.setAttribute('aria-label', `Limpar atalho: ${label}`);
      const actions = el(doc, 'div', 'kira-settings-inline kira-settings-nowrap');
      actions.append(capture, clear);
      row.append(text, actions);
      card.body.append(row);
    }
    const mode = el(doc, 'select', 'kira-settings-field');
    mode.setAttribute('aria-label', 'Modo do microfone');
    for (const [value, label] of [['toggle', 'Microfone: apertar liga/desliga'], ['hold', 'Microfone: segurar para falar']]) {
      const option = el(doc, 'option', '', label);
      option.value = value;
      mode.append(option);
    }
    mode.value = shortcuts.mic_mode || 'toggle';
    mode.addEventListener('change', () => {
      this.window.__kiraMicMode = mode.value;
      // Segurar para falar: o microfone fica desligado até apertar o atalho.
      if (mode.value === 'hold') this.window.KiraUiControl?.micShortcut?.(false);
      this.saveShortcuts({ mic_mode: mode.value });
    });
    card.body.append(mode);
    return card.card;
  }

  captureShortcut(name, button, status) {
    const doc = this.document;
    button.textContent = 'Aperte a combinação… (Esc cancela)';
    const previous = (this.state.shortcuts || {})[name] || 'Definir';
    const onKey = (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (event.code === 'Escape') {
        doc.removeEventListener('keydown', onKey, true);
        button.textContent = previous;
        return;
      }
      const combo = comboFromEvent(event);
      if (!combo) return;
      doc.removeEventListener('keydown', onKey, true);
      button.textContent = combo;
      this.saveShortcuts({ [name]: combo }, button, status);
    };
    doc.addEventListener('keydown', onKey, true);
  }

  async saveShortcuts(patch, button = null, status = null) {
    try {
      const result = await api(this.window, 'PUT', '/api/ui/settings', { shortcuts: patch });
      this.state = { ...this.state, ...result.settings };
      this.shortcutStatus = result.shortcutStatus || {};
      const name = Object.keys(patch).find((key) => key !== 'mic_mode');
      if (name && button) button.textContent = this.state.shortcuts?.[name] || 'Definir';
      if (name && status) status.textContent = this.state.shortcuts?.[name] ? (SHORTCUT_STATUS[this.shortcutStatus[name]] || '') : '';
      this.flash('Atalhos salvos.');
    } catch (error) {
      this.flash('Não consegui salvar o atalho.');
    }
  }

  async saveInterests(terms, redraw) {
    try {
      const result = await api(this.window, 'PUT', '/api/ui/interests', { terms });
      this.interests = (result.topics || []).map((t) => String(t.term || t)).filter(Boolean);
      redraw();
      this.flash('Interesses salvos.');
    } catch (error) {
      this.flash('Não consegui salvar os interesses.');
    }
  }
}
