/** Uma única conexão SSE com o coletor do backend, compartilhada por todos os widgets de sistema da página. */

const URL_BASE = '/api/ui/system-stream';

export function createStatsStream({ EventSourceImpl = globalThis.EventSource, documentRef = globalThis.document } = {}) {
  const subscribers = new Map();
  let source = null;
  let withTop = false;

  const wantsTop = () => [...subscribers.values()].some((opts) => opts.top);
  const close = () => { if (source) { source.close(); source = null; } };

  function open() {
    if (typeof EventSourceImpl !== 'function' || !subscribers.size || documentRef?.hidden) return;
    const top = wantsTop();
    if (source && top === withTop) return;
    close();
    withTop = top;
    source = new EventSourceImpl(`${URL_BASE}?top=${top ? 'true' : 'false'}`);
    source.onmessage = (event) => {
      let payload;
      try { payload = JSON.parse(event.data); } catch (_) { return; }
      subscribers.forEach((_, handler) => { try { handler(payload); } catch (_error) { /* um widget com erro não derruba os outros */ } });
    };
  }

  documentRef?.addEventListener?.('visibilitychange', () => { if (documentRef.hidden) close(); else open(); });

  return {
    /** Assina os quadros; devolve a função que cancela (e fecha a conexão quando ninguém mais assina). */
    subscribe(handler, { top = false } = {}) {
      subscribers.set(handler, { top });
      open();
      return () => { subscribers.delete(handler); if (!subscribers.size) close(); else open(); };
    },
    get active() { return Boolean(source); },
    get count() { return subscribers.size; },
  };
}
