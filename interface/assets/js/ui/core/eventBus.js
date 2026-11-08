/**
 * Pequeno emissor de eventos sem dependência de DOM.
 * @returns {{on: Function, off: Function, emit: Function, clear: Function}}
 */
export function createEventBus({ logger = globalThis.console } = {}) {
  const listeners = new Map();

  function on(eventName, handler) {
    if (typeof handler !== 'function') throw new TypeError('eventBus.on exige uma função.');
    const event = String(eventName || '').trim();
    if (!event) throw new TypeError('eventBus.on exige um nome de evento.');
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(handler);
    return () => off(event, handler);
  }

  function off(eventName, handler) {
    const event = String(eventName || '').trim();
    const current = listeners.get(event);
    if (!current) return false;
    const removed = current.delete(handler);
    if (current.size === 0) listeners.delete(event);
    return removed;
  }

  function emit(eventName, payload) {
    const event = String(eventName || '').trim();
    const current = listeners.get(event);
    if (!current) return 0;
    let delivered = 0;
    [...current].forEach((handler) => {
      try {
        handler(payload);
        delivered += 1;
      } catch (error) {
        try { logger?.error?.(`[Kira UI] handler falhou para "${event}"`, error); } catch (_) { /* logging is best-effort */ }
      }
    });
    return delivered;
  }

  function clear(eventName) {
    if (eventName == null) {
      listeners.clear();
      return;
    }
    listeners.delete(String(eventName || '').trim());
  }

  return Object.freeze({ on, off, emit, clear });
}

export const eventBus = createEventBus;
