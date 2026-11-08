// F11 na Kira: a janela em foco (principal ou extra) vai para tela cheia de verdade, cobrindo a barra de tarefas,
// como um jogo; F11 de novo volta. Quem troca é o backend (pywebview toggle_fullscreen), no PC. No celular, nada.
(function () {
  if (window.KIRA_REMOTE) return;
  const windowId = new URLSearchParams(location.search).get('window') || 'main';
  let busy = false;
  function toggle() {
    if (busy) return;
    busy = true;
    fetch('/api/ui/windows/fullscreen', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ windowId }) })
      .catch(() => {})
      .finally(() => setTimeout(() => { busy = false; }, 400));
  }
  window.KiraFullscreen = { toggle };
  window.addEventListener('keydown', (event) => {
    if (event.key !== 'F11' || event.repeat) return;
    event.preventDefault();
    event.stopPropagation();
    toggle();
  }, true);
  // Janela extra: botão no canto e duplo clique no fundo vazio também trocam (2026-10-31: o F11 não chegava nelas).
  if (windowId !== 'main') {
    const wire = () => {
      document.getElementById('sat-fullscreen')?.addEventListener('click', toggle);
      document.getElementById('sat-stage')?.addEventListener('dblclick', (event) => { if (event.target.id === 'sat-stage') toggle(); });
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire, { once: true });
    else wire();
  }
})();
