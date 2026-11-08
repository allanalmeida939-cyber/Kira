const api = async (win, method, path, body) => {
  const response = await win.fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store',
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
};

const el = (doc, tag, className = '', text = '') => {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
};

const text = (value, fallback = '—') => String(value ?? '').trim() || fallback;
const date = (value) => {
  if (!value) return 'sem data';
  try { return new Date(value).toLocaleString('pt-BR'); } catch (_) { return String(value); }
};

export class KiraMemoryManager {
  constructor({ root, window: win = globalThis.window } = {}) {
    this.root = root;
    this.window = win;
    this.document = root?.ownerDocument || win?.document;
    this.state = null;
    this.signature = '';
    this.status = null;
  }

  async load() {
    if (!this.root) return;
    try {
      const payload = await api(this.window, 'GET', '/api/memory/manager?limit=80');
      const signature = JSON.stringify(payload);
      if (signature === this.signature) return;
      this.signature = signature;
      this.state = payload;
      this.render();
    } catch (error) {
      this.root.replaceChildren(el(this.document, 'div', 'kira-memory-manager-error', 'Não consegui carregar a memória local agora.'));
    }
  }

  async refresh() {
    this.signature = '';
    await this.load();
  }

  async saveSettings(patch) {
    try {
      await api(this.window, 'PUT', '/api/memory/settings', patch);
      await this.refresh();
      this.flash('Configuração da memória salva.');
    } catch (_) { this.flash('Não consegui salvar a configuração.'); }
  }

  flash(message) {
    if (this.status) this.status.textContent = message;
  }

  card(title, subtitle, accent = '#a78bfa') {
    const card = el(this.document, 'section', 'kira-settings-card kira-memory-card');
    card.style.setProperty('--card-accent', accent);
    const head = el(this.document, 'header', 'kira-settings-head');
    head.append(el(this.document, 'h3', 'kira-settings-title', title));
    if (subtitle) head.append(el(this.document, 'p', 'kira-settings-sub', subtitle));
    const body = el(this.document, 'div', 'kira-settings-body');
    card.append(head, body);
    return { card, body };
  }

  metric(label, value) {
    const item = el(this.document, 'div', 'kira-memory-metric');
    item.append(el(this.document, 'strong', '', text(value, '0')), el(this.document, 'span', '', label));
    return item;
  }

  render() {
    const doc = this.document;
    const data = this.state || {};
    const summary = data.summary || {};
    const settings = summary.settings || {};
    this.root.replaceChildren();
    this.root.classList.add('kira-memory-manager');
    const scroll = el(doc, 'div', 'kira-settings-scroll kira-memory-scroll');
    const grid = el(doc, 'div', 'kira-settings-grid kira-memory-grid');

    const overview = this.card('O que a Kira sabe de você', 'Suas preferências e os jeitos de fazer que você ensinou. Tudo aqui pode ser editado ou esquecido.', '#c084fc');
    const metrics = el(doc, 'div', 'kira-memory-metrics');
    metrics.append(
      this.metric('preferências', summary.preferences),
      this.metric('procedimentos ensinados', summary.procedures),
    );
    overview.body.append(metrics);
    grid.append(overview.card);

    const controls = this.card('Controle do aprendizado', 'Desative o aprendizado automático ou limite a retenção sem apagar preferências explícitas.', '#34d399');
    const auto = el(doc, 'button', `kira-switch${settings.auto_learning_enabled ? ' enabled' : ''}`);
    auto.type = 'button'; auto.setAttribute('role', 'switch'); auto.setAttribute('aria-checked', settings.auto_learning_enabled ? 'true' : 'false');
    auto.addEventListener('click', () => this.saveSettings({ auto_learning_enabled: !settings.auto_learning_enabled }));
    const autoRow = el(doc, 'div', 'kira-settings-row');
    const autoText = el(doc, 'div', 'kira-settings-row-text');
    autoText.append(el(doc, 'div', 'kira-settings-label', 'Aprendizado automático'), el(doc, 'div', 'kira-settings-help', 'Registra resultados de ferramentas, mas nunca trata uma ação não confirmada como sucesso.'));
    autoRow.append(autoText, auto);
    const tool = el(doc, 'button', `kira-switch${settings.record_tool_outcomes ? ' enabled' : ''}`);
    tool.type = 'button'; tool.setAttribute('role', 'switch'); tool.setAttribute('aria-checked', settings.record_tool_outcomes ? 'true' : 'false');
    tool.addEventListener('click', () => this.saveSettings({ record_tool_outcomes: !settings.record_tool_outcomes }));
    const toolRow = el(doc, 'div', 'kira-settings-row');
    const toolText = el(doc, 'div', 'kira-settings-row-text');
    toolText.append(el(doc, 'div', 'kira-settings-label', 'Registrar resultados das ferramentas'), el(doc, 'div', 'kira-settings-help', 'Inclui TV, navegador, casa, programas e endpoints que retornam confirmação de sucesso/falha.'));
    toolRow.append(toolText, tool);
    const retention = el(doc, 'input', 'kira-settings-field');
    retention.type = 'number'; retention.min = '1'; retention.max = '3650'; retention.placeholder = 'sem limite'; retention.value = settings.retention_days || '';
    retention.setAttribute('aria-label', 'Retenção de experiências em dias');
    const retentionButton = el(doc, 'button', 'kira-settings-button', 'Salvar retenção');
    retentionButton.type = 'button'; retentionButton.addEventListener('click', () => this.saveSettings({ retention_days: retention.value || null }));
    // Simples: o switch de aprender + um campo para ensinar algo; o resto vai para o Avançado recolhido.
    const prefForm = el(doc, 'form', 'kira-memory-form is-inline');
    const prefContent = el(doc, 'input', 'kira-settings-field'); prefContent.required = true; prefContent.maxLength = 500; prefContent.placeholder = 'Ensine algo: "quando eu pedir série na TV, use a Netflix"'; prefContent.setAttribute('aria-label', 'Ensinar uma preferência');
    const save = el(doc, 'button', 'kira-settings-button', 'Guardar'); save.type = 'submit';
    prefForm.append(prefContent, save);
    prefForm.addEventListener('submit', async (event) => {
      event.preventDefault(); save.disabled = true;
      try {
        await api(this.window, 'POST', '/api/memory/preferences', { content: prefContent.value, scope: '', context: {}, source: 'settings', explicit: true });
        prefContent.value = '';
        await this.refresh(); this.flash('Preferência guardada.');
      } catch (_) { this.flash('Não consegui guardar a preferência.'); }
      save.disabled = false;
    });
    overview.body.append(autoRow, prefForm);
    if ((data.preferences || []).length) { // cartão só aparece quando há o que mostrar
      const prefs = this.card('Preferências salvas', 'Edite ou esqueça.', '#fbbf24');
      this.renderPreferences(prefs.body, data.preferences || []);
      grid.append(prefs.card);
    }
    const retentionRow = el(doc, 'div', 'kira-settings-row');
    retentionRow.append(el(doc, 'div', 'kira-settings-label', 'Guardar experiências por (dias)'), retention, retentionButton);

    // Diagnóstico técnico (endpoints, contagens, evidências) fica recolhido: não é memória pessoal.
    const advanced = el(doc, 'details', 'kira-memory-advanced');
    const advTitle = el(doc, 'summary', 'kira-settings-label', 'Diagnóstico técnico (avançado)');
    const techMetrics = el(doc, 'div', 'kira-memory-metrics');
    techMetrics.append(
      this.metric('experiências', summary.experiences),
      this.metric('acertos confirmados', summary.confirmed_successes),
      this.metric('falhas confirmadas', summary.confirmed_failures),
    );
    const advGrid = el(doc, 'div', 'kira-settings-grid kira-memory-grid');
    advanced.append(advTitle, toolRow, retentionRow, techMetrics, el(doc, 'p', 'kira-settings-help', 'A confiança só sobe com resultado confirmado. Tentativas ambíguas ficam como desconhecidas.'), advGrid);
    advanced.addEventListener('toggle', () => {
      if (!advanced.open || advGrid.childElementCount) return; // só monta a lista técnica quando abrir
      const patterns = this.card('Padrões observados', 'Resumo de acertos e falhas confirmados.', '#22d3ee');
      this.renderPatterns(patterns.body, data.patterns || []);
      const experiences = this.card('Experiências recentes', 'O que foi tentado e com qual evidência.', '#a78bfa');
      this.renderExperiences(experiences.body, data.experiences || []);
      const procedures = this.card('Procedimentos aprendidos', 'Estratégias que deram certo/errado.', '#60a5fa');
      this.renderProcedures(procedures.body, data.procedures || []);
      advGrid.append(procedures.card, patterns.card, experiences.card);
    });

    scroll.append(grid, advanced);
    this.status = el(doc, 'div', 'kira-settings-status', 'Memória local sincronizada.');
    this.root.append(scroll, this.status);
  }

  renderPreferences(parent, items) {
    if (!items.length) { parent.append(el(this.document, 'div', 'kira-settings-help', 'Nenhuma preferência explícita salva.')); return; }
    items.forEach((item) => {
      const row = el(this.document, 'div', 'kira-memory-row');
      const details = el(this.document, 'div', 'kira-memory-row-main');
      details.append(el(this.document, 'strong', '', text(item.preference)), el(this.document, 'span', 'kira-memory-meta', `${text(item.scope, 'geral')} · ${item.explicit ? 'explícita' : 'inferida'} · ${date(item.updated_at)}`));
      const remove = el(this.document, 'button', 'kira-settings-button kira-settings-icon-button', '×'); remove.type = 'button'; remove.title = 'Esquecer preferência';
      remove.addEventListener('click', async () => { if (!this.window.confirm?.('Esquecer esta preferência?')) return; await api(this.window, 'DELETE', `/api/memory/preference/${item.id}`); await this.refresh(); });
      row.append(details, remove); parent.append(row);
    });
  }

  renderProcedures(parent, items) {
    if (!items.length) { parent.append(el(this.document, 'div', 'kira-settings-help', 'Ainda não há procedimentos confirmados.')); return; }
    items.forEach((item) => {
      const row = el(this.document, 'div', 'kira-memory-row kira-memory-procedure');
      const details = el(this.document, 'div', 'kira-memory-row-main');
      const state = `${item.success_count || 0} acerto(s) · ${item.failure_count || 0} falha(s) · confiança ${Number(item.confidence || 0).toFixed(2)}`;
      details.append(el(this.document, 'strong', '', text(item.goal)), el(this.document, 'span', 'kira-memory-meta', state));
      details.append(el(this.document, 'div', 'kira-memory-steps', (item.steps || []).slice(0, 6).map((step, index) => `${index + 1}. ${step}`).join('  ')));
      const remove = el(this.document, 'button', 'kira-settings-button kira-settings-icon-button', '×'); remove.type = 'button'; remove.title = 'Redefinir procedimento';
      remove.addEventListener('click', async () => { if (!this.window.confirm?.('Redefinir este procedimento?')) return; await api(this.window, 'DELETE', `/api/memory/procedure/${item.id}`); await this.refresh(); });
      row.append(details, remove); parent.append(row);
    });
  }

  renderPatterns(parent, items) {
    if (!items.length) { parent.append(el(this.document, 'div', 'kira-settings-help', 'Ainda não há padrões confirmados.')); return; }
    items.forEach((item) => parent.append(el(this.document, 'div', 'kira-memory-row', `${text(item.goal)} — ${item.successes || 0} acerto(s), ${item.failures || 0} falha(s), taxa de sucesso ${Math.round(Number(item.success_rate || 0) * 100)}%`)));
  }

  renderExperiences(parent, items) {
    if (!items.length) { parent.append(el(this.document, 'div', 'kira-settings-help', 'Nenhuma experiência registrada ainda.')); return; }
    items.slice(0, 30).forEach((item) => {
      const state = item.status === 'success' ? 'confirmado' : item.status === 'failure' ? 'falhou' : item.status === 'unknown' ? 'não confirmado' : item.status;
      const row = el(this.document, 'div', 'kira-memory-row', `${text(item.goal)} — ${state} · ${date(item.created_at)}`);
      if (item.evidence || item.error || item.result) row.append(el(this.document, 'span', 'kira-memory-meta', text(item.error || item.result || item.evidence)));
      parent.append(row);
    });
  }
}
