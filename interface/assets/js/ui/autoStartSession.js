// 24/7 (2026-11-07): reaberta por queda ou ao ligar o PC (Kira.bat), a sessão inicia sozinha, sem clique.
(function autoStartSession() {
  if (window.KIRA_REMOTE || window.KIRA_TV) return; // só a janela do PC
  fetch('/api/auto-start-session', { cache: 'no-store' }).then((r) => r.json()).then((data) => {
    if (!data || !data.auto) return;
    const startedAt = Date.now();
    const timer = setInterval(() => {
      const btn = document.getElementById('start-btn');
      if (Date.now() - startedAt > 10 * 60 * 1000) { clearInterval(timer); return; }
      if (!btn || btn.disabled || btn.offsetParent === null) return;
      clearInterval(timer);
      console.info('AUTO_START_SESSION_CLICK');
      btn.click();
      fetch('/api/auto-start-session/done', { method: 'POST' }).catch(() => {});
    }, 2000);
  }).catch(() => {});
})();
