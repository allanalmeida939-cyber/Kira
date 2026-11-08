// Celular: as janelas extras do PC ("Janela 2"...) viram botões abaixo do relógio; tocar abre a página daquela janela
// numa aba nova do navegador. Só no modo celular (window.KIRA_REMOTE, posto pelo portão). Nomes só como texto.

const REFRESH_MS = 15000;

/** Janelas extras abertas no PC -> rótulo e endereço da página da janela. */
export function windowLinks(windows = []) {
  return (Array.isArray(windows) ? windows : [])
    .filter((item) => /^w\d+$/.test(String(item?.windowId || '')) && item.windowId !== 'w1')
    .map((item) => ({
      label: String(item.name || item.title || `Janela ${String(item.windowId).slice(1)}`).slice(0, 30),
      href: `/?kiraui=1&window=${encodeURIComponent(item.windowId)}&role=satellite`,
    }));
}

export function createRemoteWindows({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  const doc = documentRef;
  const box = doc.createElement('nav');
  box.id = 'remote-windows';
  box.className = 'remote-windows';
  box.setAttribute('aria-label', 'Janelas da Kira no PC');
  doc.body.appendChild(box);

  function render(links) {
    box.replaceChildren(...links.map((link) => {
      const anchor = doc.createElement('a');
      anchor.href = link.href;
      anchor.target = '_blank';
      anchor.rel = 'noopener';
      anchor.textContent = link.label;
      return anchor;
    }));
    box.hidden = !links.length;
  }

  async function refresh() {
    if (doc.hidden) return;
    try {
      const response = await windowRef.fetch('/api/ui/windows', { cache: 'no-store' });
      render(windowLinks((await response.json())?.windows));
    } catch (_) {
      /* continua com a lista anterior */
    }
  }

  render([]);
  refresh();
  windowRef.setInterval(refresh, REFRESH_MS);
  return { refresh, render };
}

if (globalThis.window?.KIRA_REMOTE && globalThis.document?.body && !globalThis.window.KiraRemoteWindows) {
  globalThis.window.KiraRemoteWindows = createRemoteWindows();
}
