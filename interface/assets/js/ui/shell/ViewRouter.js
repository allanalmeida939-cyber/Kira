const VIEW_IDS = Object.freeze(['main', 'chat']);

export class ViewRouter {
  constructor({ bus = null, store = null, windowManager = null, documentRef = globalThis.document } = {}) {
    this.bus = bus;
    this.store = store;
    this.windowManager = windowManager;
    this.document = documentRef;
    this.view = 'main';
    this.document?.documentElement?.dataset && (this.document.documentElement.dataset.kuiView = this.view);
  }

  go(viewId) {
    const nextView = String(viewId || '').trim().toLowerCase();
    if (!VIEW_IDS.includes(nextView)) return false;
    if (this.view === nextView) {
      this.windowManager?.showView?.(nextView);
      return true;
    }
    const previous = this.view;
    this.view = nextView;
    this.store?.setView?.(nextView);
    this.windowManager?.showView?.(nextView);
    this.document?.documentElement?.dataset && (this.document.documentElement.dataset.kuiView = nextView);
    this.bus?.emit?.('view:change', { from: previous, to: nextView, view: nextView });
    return true;
  }

  current() {
    return this.view;
  }
}

export const VIEW_ROUTER_IDS = VIEW_IDS;
