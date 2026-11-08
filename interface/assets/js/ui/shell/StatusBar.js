import { clear, create, safeText } from '../views/panelDom.js';

const STATE_LABELS = Object.freeze({ thinking: 'Pensando…', searching: 'Pesquisando…', idle: 'Pronto' });

export class StatusBar {
  constructor({ bus, documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
    this.bus = bus;
    this.document = documentRef;
    this.window = windowRef;
    this.root = null;
    this.timers = { voice: null, media: null };
    this.states = { voice: null, media: null };
    this.currentScope = 'voice';
    this.unsubscribe = null;
  }

  mount(parent = this.document?.body) {
    if (!parent || this.root) return this;
    this.root = create(this.document, 'aside', 'kui-status-bar');
    this.root.hidden = true;
    this.root.setAttribute('aria-live', 'polite');
    this.root.appendChild(create(this.document, 'span', 'kui-status-bar-text'));
    this.root.appendChild(create(this.document, 'ol', 'kui-status-bar-steps'));
    parent.appendChild(this.root);
    this.unsubscribe = this.bus?.on?.('action:status', (action) => this.show(action)) || (() => {});
    return this;
  }

  show(action = {}) {
    if (!this.root) return;
    const scope = action.scope === 'media' ? 'media' : 'voice';
    if (this.timers[scope] != null) (this.window?.clearTimeout || clearTimeout)(this.timers[scope]);
    this.timers[scope] = null;
    const state = safeText(action.state, 'idle');
    const label = safeText(action.text, STATE_LABELS[state] || 'Pronto');
    this.states[scope] = { ...action, state, text: label };
    this.currentScope = scope;
    this.root.dataset.state = state;
    this.root.dataset.scope = scope;
    this.root.hidden = false;
    const textNode = this.root.querySelector?.('.kui-status-bar-text');
    if (textNode) textNode.textContent = label;
    const steps = this.root.querySelector?.('.kui-status-bar-steps');
    if (steps) {
      clear(steps);
      (Array.isArray(action.steps) ? action.steps : []).forEach((step) => {
        const item = create(this.document, 'li', step?.done ? 'is-done' : '', `${step?.done ? '✓ ' : ''}${safeText(step?.label, 'etapa')}`);
        steps.appendChild(item);
      });
    }
    if (state === 'idle') {
      const delay = scope === 'media' ? 3000 : 1500;
      this.timers[scope] = (this.window?.setTimeout || setTimeout)(() => {
        this.timers[scope] = null;
        if (this.root && this.currentScope === scope) this.root.hidden = true;
      }, delay);
    }
  }

  destroy() {
    Object.keys(this.timers).forEach((scope) => {
      if (this.timers[scope] != null) (this.window?.clearTimeout || clearTimeout)(this.timers[scope]);
      this.timers[scope] = null;
    });
    this.unsubscribe?.();
    this.unsubscribe = null;
    this.root?.remove?.();
    this.root = null;
  }
}
