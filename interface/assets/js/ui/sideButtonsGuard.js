// Guarda dos botões da lateral (WhatsApp, Navegador, Música): o do WhatsApp sumiu depois de abrir e fechar a aba
// (2026-10-30, sem reproduzir de novo). Com nenhuma aba aberta, os três ficam visíveis e dentro da tela; se algum
// estiver escondido, transparente ou fora da tela, volta ao lugar e o motivo vai para o console (KIRA_SIDE_BUTTON_FIXED).
(function () {
  if (window.KIRA_REMOTE) return;
  const IDS = { 'drawer-whatsapp-btn-float': 'whatsapp', 'drawer-nav-btn-float': 'browser', 'drawer-music-btn-float': 'music' };
  const DRAWERS = '#whatsapp-drawer.open, #browser-drawer.open, #music-drawer.open';

  function featureOff(module) {
    const features = window.KiraFeatures;
    return Boolean(features?.loaded && features.shouldShow && !features.shouldShow(module));
  }

  function check(source) {
    const shell = document.querySelector('.app-shell');
    // O Olho de Deus oculta os atalhos por desenho enquanto ocupa a tela toda.
    // Não tente restaurá-los nesse estado: além de não vencer o CSS com
    // !important, isso gerava KIRA_SIDE_BUTTON_FIXED a cada intervalo.
    if (shell?.dataset.uiStage === 'landing'
      || document.body?.classList.contains('kira-godseye-open')
      || document.querySelector(DRAWERS)) return;
    Object.entries(IDS).forEach(([id, module]) => {
      const button = document.getElementById(id);
      if (!button || featureOff(module)) return;
      const style = getComputedStyle(button);
      const rect = button.getBoundingClientRect();
      const reasons = [];
      if (button.hidden) reasons.push('hidden');
      if (style.display === 'none') reasons.push('display');
      if (style.visibility === 'hidden') reasons.push('visibility');
      if (Number(style.opacity) < 0.05) reasons.push(`opacity=${style.opacity}`);
      if (rect.width && (rect.left >= innerWidth - 8 || rect.right <= 8 || rect.top >= innerHeight - 8 || rect.bottom <= 8)) reasons.push('fora_da_tela');
      if (!reasons.length) return;
      button.hidden = false;
      ['display', 'visibility', 'opacity'].forEach((prop) => button.style.removeProperty(prop));
      if (reasons.includes('fora_da_tela')) button.style.removeProperty('translate');
      console.warn(`KIRA_SIDE_BUTTON_FIXED id=${id} motivo=${reasons.join(',')} origem=${source}`);
    });
  }

  document.addEventListener('click', () => setTimeout(() => check('clique'), 700), true);
  setInterval(() => check('intervalo'), 5000);
  window.KiraSideButtonsGuard = { check };
})();
