/* Voz da Kira na TV (2026-11-06): com "fala pela TV" ligado, /audio-token devolve um token SÓ-OUVINTE da sala de voz;
   a página entra, toca só a faixa de áudio da Kira (agente) e sai quando o modo desliga (204). ES2017, sem ?. nem ??.
   Navegador da TV pode bloquear som sem gesto: aparece "aperte OK" e o som começa no primeiro botão do controle. */
(function () {
  var room = null; var busy = false; var hint = null;
  var LK = function () { return window.LivekitClient; };
  // avisa o PC: 'playing' = a faixa da Kira está tocando aqui (só então o PC fica mudo); 'blocked' = autoplay barrou
  function playing() { // algum áudio da Kira tocando de verdade (o elemento antigo da sala anterior não conta)
    var all = document.querySelectorAll('audio[data-kira]');
    for (var i = 0; i < all.length; i++) if (!all[i].paused && all[i].srcObject) return true;
    return false;
  }
  function report(s) { try { fetch('/audio-ok?s=' + s, { cache: 'no-store' }).catch(function () {}); } catch (e) { /* sem fetch */ } }

  function showHint(on) {
    if (on && !hint) {
      hint = document.createElement('div');
      hint.textContent = 'Aperte OK no controle para ouvir a Kira';
      hint.style.cssText = 'position:fixed;left:50%;bottom:6%;transform:translateX(-50%);padding:18px 34px;border-radius:40px;'
        + 'background:#2e1065;color:#f5f3ff;font:600 30px sans-serif;z-index:99';
      document.body.appendChild(hint);
    } else if (!on && hint) { hint.remove(); hint = null; }
  }

  function unlock() {
    if (!room) return;
    room.startAudio().then(function () {
      showHint(false);
      if (playing()) report('playing');
    }).catch(function () {});
  }
  document.addEventListener('keydown', unlock); document.addEventListener('click', unlock);

  function isAgent(p) { var id = String((p && p.identity) || ''); return id.indexOf('tv-') !== 0 && id !== 'phone' && !/^(user|alan|celular)/i.test(id); }

  function join(cfg) {
    var lk = LK(); if (!lk || !lk.Room) return;
    room = new lk.Room({ adaptiveStream: false });
    room.on(lk.RoomEvent.TrackSubscribed, function (track, pub, participant) {
      if (track.kind !== 'audio' || !isAgent(participant)) return;
      var el = track.attach(); el.autoplay = true; el.dataset.kira = '1'; document.body.appendChild(el);
      el.addEventListener('playing', function () { report('playing'); });
      var p = el.play(); if (p && p.catch) p.catch(function () { showHint(true); report('blocked'); });
    });
    // texto da fala sincronizado com o som da TV: as imagens da história trocam quando ela FALA do momento (antes, sem
    // isso, a TV trocava a cada 10 s no escuro e as imagens ficavam atrasadas/fora de ordem)
    room.on(lk.RoomEvent.TranscriptionReceived, function (segments, participant) {
      if (participant && !isAgent(participant)) return;
      (segments || []).forEach(function (seg) {
        if (seg && seg.text) window.dispatchEvent(new CustomEvent('kira:spoken', { detail: { id: String(seg.id || ''), text: String(seg.text), final: !!seg.final } }));
      });
    });
    room.on(lk.RoomEvent.TrackUnsubscribed, function (track) { track.detach().forEach(function (el) { el.remove(); }); });
    room.on(lk.RoomEvent.AudioPlaybackStatusChanged, function () { showHint(!room.canPlaybackAudio); });
    room.on(lk.RoomEvent.Disconnected, function () { room = null; });
    room.connect(cfg.url, cfg.token, { autoSubscribe: true }).then(function () {
      window.__kiraTvAudio = 'conectado';
      if (!room.canPlaybackAudio) showHint(true);
    }).catch(function (e) { window.__kiraTvAudio = 'erro: ' + e; room = null; });
  }

  function leave() {
    if (room) { room.disconnect(); room = null; }
    var old = document.querySelectorAll('audio[data-kira]'); // sobrava um <audio> parado da sala anterior
    for (var i = 0; i < old.length; i++) old[i].remove();
    showHint(false); window.__kiraTvAudio = 'desligado';
  }

  function poll() {
    if (busy) return; busy = true;
    fetch('/audio-token', { cache: 'no-store' }).then(function (r) { return r.status === 200 ? r.json() : null; })
      .then(function (cfg) {
        if (!(cfg && cfg.token)) { leave(); return; }
        if (!room) { join(cfg); return; }
        if (playing()) report('playing'); // já na sala: confirma de novo para o PC poder mutar
      })
      .catch(function () {}).then(function () { busy = false; });
  }
  // na hora: o PC manda {"t":"audio","on":...} pelos eventos; o polling de 3 s fica de reserva
  try {
    var es = new EventSource('/events');
    es.onmessage = function (e) {
      if (e.data.indexOf('"audio"') < 0) return; // ignora estado/painéis sem parsear
      var msg = null; try { msg = JSON.parse(e.data); } catch (err) { return; }
      if (msg && msg.t === 'audio') { if (msg.on) poll(); else leave(); }
    };
  } catch (e) { /* sem EventSource: só o polling */ }
  setInterval(poll, 3000); poll();
})();
