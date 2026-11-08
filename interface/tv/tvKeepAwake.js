/* Mantém a TV LG acordada (2026-11-06): sem vídeo tocando, o webOS achava que não havia nada acontecendo e entrava
   em descanso (tela apagada com relógio). Um vídeo de 16x16, mudo, em loop, da cor do fundo, faz a TV "ver" mídia
   tocando, como no YouTube. Não pode ser display:none/visibility:hidden (o navegador pausa): fica 2x2 px no canto,
   quase transparente (2026-11-07: só toca na LG se o servidor responder Range/206 e o vídeo for 320x180 H.264
   baseline; 16x16 com 200 inteiro dava erro de decodificação 3). Wake Lock quando existir; replay se pausar; ao voltar da espera, recarrega a página
   (reconecta /events e redesenha). ES2017. */
(function () {
  var v = document.createElement('video');
  v.src = '/static/awake.mp4';
  v.muted = true; v.loop = true; v.autoplay = true;
  v.setAttribute('muted', ''); v.setAttribute('playsinline', ''); v.setAttribute('aria-hidden', 'true');
  v.style.cssText = 'position:fixed;right:0;bottom:0;width:2px;height:2px;opacity:0.01;pointer-events:none;z-index:-1';
  document.body.appendChild(v);
  function play() { var p = v.play(); if (p && p.catch) p.catch(function () {}); }
  play();
  v.addEventListener('pause', function () { setTimeout(play, 500); });
  setInterval(function () { if (v.paused || v.ended) play(); }, 20000); // garantia se a TV pausar sem avisar

  var lock = null;
  function wake() {
    try { if (navigator.wakeLock && !lock) navigator.wakeLock.request('screen').then(function (l) { lock = l; l.addEventListener('release', function () { lock = null; }); }).catch(function () {}); } catch (e) { /* sem Wake Lock */ }
  }
  wake();

  var hiddenAt = 0;
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { hiddenAt = Date.now(); return; }
    play(); wake();
    // voltou de espera depois de um tempo: recarrega para reconectar /events e redesenhar limpo
    if (hiddenAt && Date.now() - hiddenAt > 30000) location.reload();
  });
})();
