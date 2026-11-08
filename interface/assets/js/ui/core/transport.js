function log(logger, level, message, error) {
  try { logger?.[level]?.(`[Kira UI] ${message}`, error || ''); } catch (_) { /* best-effort */ }
}

/** Apresentação simulada: seções chegando aos poucos, fontes, navegação e um pedido cancelado pelo seguinte. */
function presentationDemo() {
  const base = { presentationId: 'demo-presentation', requestId: 'rq-demo-1' };
  const source = { id: 'wiki', name: 'Wikipedia: Descobrimento do Brasil', url: 'https://pt.wikipedia.org/wiki/Descobrimento_do_Brasil', accessedAt: new Date().toISOString() };
  return [
    [200, { type: 'present', ...base, title: 'Descobrimento do Brasil (demonstração)', status: 'preparing_query', sections: [
      { id: 'resumo', type: 'text', heading: 'Resumo', status: 'pending' },
      { id: 'linha', type: 'timeline', heading: 'Linha do tempo', status: 'pending' },
      { id: 'fontes', type: 'sources', heading: 'Fontes', status: 'pending' },
    ] }],
    [500, { type: 'present_patch', ...base, status: 'researching' }],
    [900, { type: 'present_patch', ...base, sources: [source], sections: [{ id: 'resumo', status: 'ready', data: { body: 'Em abril de 1500 a esquadra de Pedro Álvares Cabral chegou ao litoral da atual Bahia.', points: ['Chegada em 22 de abril', 'Primeira missa em 26 de abril'] } }] }],
    [1400, { type: 'present_patch', ...base, sections: [{ id: 'linha', status: 'ready', data: { events: [{ date: '9 mar 1500', label: 'A esquadra parte de Lisboa', order: 1 }, { date: '22 abr 1500', label: 'Avistamento do Monte Pascoal', order: 2 }, { date: '26 abr 1500', label: 'Primeira missa', order: 3 }] } }] }],
    [1700, { type: 'present_patch', ...base, status: 'organizing', sections: [{ id: 'fontes', status: 'ready' }] }],
    [1900, { type: 'present_patch', ...base, status: 'ready', activeSectionId: 'linha' }],
    [2600, { type: 'present', presentationId: 'demo-cancel', requestId: 'rq-demo-2', title: 'Pedido substituído (demonstração)', status: 'researching', sections: [{ id: 'resumo', type: 'text', heading: 'Resumo', status: 'pending' }] }],
    [3000, { type: 'present_patch', presentationId: 'demo-cancel', requestId: 'rq-demo-2', status: 'cancelled' }],
    [3200, { type: 'present_patch', presentationId: 'demo-cancel', requestId: 'rq-demo-old', sections: [{ id: 'resumo', status: 'ready', data: { body: 'evento atrasado: nunca aparece' } }] }],
  ];
}

export class MockTransport {
  constructor({ logger = globalThis.console } = {}) {
    this.logger = logger;
    this.onRaw = null;
    this.started = false;
    this.demoTimers = [];
  }
  start(onRaw) {
    this.onRaw = typeof onRaw === 'function' ? onRaw : null;
    this.started = true;
    return this;
  }
  stop() {
    this.started = false;
    this.onRaw = null;
    this.demoTimers.forEach((timer) => clearTimeout(timer));
    this.demoTimers = [];
  }
  push(raw) {
    if (!this.started || !this.onRaw) return false;
    this.onRaw(raw);
    return true;
  }
  demo() {
    if (!this.started) return false;
    const galleryItems = [
      ['Montanha', 'https://commons.wikimedia.org/wiki/Special:FilePath/Fronalpstock_big.jpg'],
      ['Terra', 'https://commons.wikimedia.org/wiki/Special:FilePath/Blue_Marble%2C_AS17-148-22727.jpg'],
      ['Aurora', 'https://commons.wikimedia.org/wiki/Special:FilePath/Aurora_Australis.jpg'],
      ['Rosa', 'https://commons.wikimedia.org/wiki/Special:FilePath/Red_Rose.jpg'],
      ['Lua', 'https://commons.wikimedia.org/wiki/Special:FilePath/FullMoon2010.jpg'],
      ['Oceano', 'https://commons.wikimedia.org/wiki/Special:FilePath/Oceanus.jpg'],
    ].map(([title, imageUrl]) => ({ title, imageUrl, source: 'Wikimedia Commons', license: 'CC BY-SA / domínio público' }));
    [
      { type: 'open_panel', kind: 'gallery', id: 'demo-gallery', title: 'Galeria de demonstração', props: { query: 'Wikimedia Commons', items: galleryItems } },
      { type: 'open_panel', kind: 'video', id: 'demo-video', title: 'Vídeo de demonstração', props: { videoId: 'dQw4w9WgXcQ', items: [{ videoId: 'dQw4w9WgXcQ', title: 'Exemplo' }] } },
      { type: 'open_panel', kind: 'article', id: 'demo-article', title: 'Artigo de demonstração', props: { title: 'Leitura simulada', excerpt: 'Conteúdo de exemplo para validar tipografia, quebra e fonte.', source: 'Wikimedia Commons', url: 'https://commons.wikimedia.org/' } },
      { type: 'open_panel', kind: 'search-log', id: 'demo-search-log', title: 'Registro de pesquisa', props: { items: [{ query: 'paisagem', types: ['imagens'], time: 'agora', ok: true }, { query: 'fonte', types: ['artigos'], time: 'agora', ok: false }] } },
    ].forEach((action) => this.push(action));
    this.demoTimers = [
      setTimeout(() => this.push({ type: 'status', state: 'searching', text: 'Procurando imagens…', steps: [{ label: 'Consultar fontes', done: true }, { label: 'Organizar resultados', done: false }] }), 60),
      setTimeout(() => this.push({ type: 'status', state: 'searching', text: 'Lendo artigos…', steps: [{ label: 'Consultar fontes', done: true }, { label: 'Organizar resultados', done: true }] }), 700),
      setTimeout(() => this.push({ type: 'status', state: 'idle', text: '✓', steps: [{ label: 'Concluído', done: true }] }), 1400),
      ...presentationDemo().map(([delay, action]) => setTimeout(() => this.push(action), delay)),
    ];
    return true;
  }
}

export class WsTransport {
  constructor({ url = null, logger = globalThis.console, WebSocketImpl = globalThis.WebSocket } = {}) {
    // wss quando a página veio por HTTPS (celular pelo Tailscale): ws:// numa página segura é bloqueado
    const scheme = globalThis.location?.protocol === 'https:' ? 'wss' : 'ws';
    this.url = url || (globalThis.location ? `${scheme}://${globalThis.location.host}/ws/ui-actions` : 'ws://127.0.0.1/ws/ui-actions');
    this.logger = logger;
    this.WebSocketImpl = WebSocketImpl;
    this.socket = null;
    this.onRaw = null;
    this.timer = null;
    this.stopped = true;
    this.delay = 1000;
  }
  start(onRaw) {
    this.onRaw = typeof onRaw === 'function' ? onRaw : null;
    this.stopped = false;
    this.delay = 1000;
    this._connect();
    return this;
  }
  stop() {
    this.stopped = true;
    if (this.timer != null) clearTimeout(this.timer);
    this.timer = null;
    try { this.socket?.close?.(); } catch (_) { /* best-effort */ }
    this.socket = null;
  }
  _connect() {
    if (this.stopped || typeof this.WebSocketImpl !== 'function') {
      if (!this.stopped) this._schedule(true);
      return;
    }
    try {
      const socket = new this.WebSocketImpl(this.url);
      this.socket = socket;
      socket.onopen = () => { this.delay = 1000; log(this.logger, 'info', `transport WebSocket conectado: ${this.url}`); };
      socket.onmessage = (event) => { try { this.onRaw?.(JSON.parse(event.data)); } catch (error) { log(this.logger, 'warn', 'ação WebSocket inválida ignorada.', error); } };
      socket.onerror = (error) => log(this.logger, 'debug', 'endpoint /ws/ui-actions indisponível; mantendo reconexão.', error);
      socket.onclose = () => { this.socket = null; if (!this.stopped) this._schedule(); };
    } catch (error) {
      log(this.logger, 'debug', 'não foi possível abrir /ws/ui-actions; mantendo o app funcional.', error);
      this._schedule();
    }
  }
  _schedule(longBackoff = false) {
    if (this.stopped || this.timer != null) return;
    const delay = longBackoff ? Math.max(this.delay, 15000) : this.delay;
    this.delay = Math.min(Math.max(this.delay * 2, 1000), 15000);
    this.timer = setTimeout(() => { this.timer = null; this._connect(); }, delay);
  }
}

/** @typedef {{start(onRaw: Function): object, stop(): void}} ActionTransport */
