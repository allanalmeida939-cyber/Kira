// Aba Música · Vitrine viva (modelo escolhido em 2026-11-01): destaque "Continue ouvindo" com a cor da capa e fileiras de
// sugestões (música e vídeos) da busca da Kira. Fica no lugar do "Nenhuma música tocada"; resultados guardados 6 h (cota do YouTube).
(() => {
  const KEY = 'kira.music.vitrine.v1';
  const TTL = 6 * 3600e3;
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const json = (url) => fetch(url, { cache: 'no-store' }).then((r) => r.json()).catch(() => null);

  async function rows(playlists) {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || 'null');
      if (saved && Date.now() - saved.at < TTL) return saved.rows;
    } catch (_) { /* sem storage: busca de novo */ }
    const taste = playlists[0]?.name;
    const asks = [['Pra você agora · Música', 'músicas em alta no Brasil hoje'], taste && [`Mais ${taste}`, `${taste} lançamentos`], ['Vídeos em alta', 'vídeos em alta no Brasil hoje']].filter(Boolean);
    const found = await Promise.all(asks.map(([title, q]) => json(`/api/music/search?q=${encodeURIComponent(q)}&max_results=8`)
      .then((r) => ({ title, items: r?.data?.results || [] }))));
    const out = found.filter((row) => row.items.length);
    try { localStorage.setItem(KEY, JSON.stringify({ at: Date.now(), rows: out })); } catch (_) { /* vale até fechar */ }
    return out;
  }

  async function render() {
    const host = document.querySelector('#mus-recents-section');
    if (!host) return;
    host.querySelector('#mus-vitrine')?.remove(); // refaz a cada abertura da aba (conta conectada, login no YouTube...)
    const box = document.createElement('div');
    box.id = 'mus-vitrine';
    host.prepend(box);
    const playlists = (await json('/api/music/playlists'))?.data?.playlists || [];
    const first = playlists.find((p) => p.items?.length);
    const cover = first?.items?.[0]?.thumbnail || '';
    box.innerHTML = first ? `<div class="vit-hero" style="--cover:url('${esc(cover)}')"><img src="${esc(cover)}" alt=""><div><small>CONTINUE OUVINDO</small>
      <b>${esc(first.name)}</b><span>${first.items.length} faixas · sua playlist</span><button type="button" data-pl="${esc(first.id)}">▶ Tocar</button></div></div>` : '';
    box.querySelector('[data-pl]')?.addEventListener('click', (e) => globalThis.musPlayPlaylistById?.(e.currentTarget.dataset.pl));
    // Conta Google conectada: canais seguidos e curtidas vêm primeiro; sem conta, um botão para conectar
    const google = await json('/api/google/status');
    const mine = google?.connected ? (await json('/api/youtube/mine'))?.rows || [] : [];
    if (google && !google.connected) {
      box.insertAdjacentHTML('beforeend', `<button type="button" class="vit-connect">▶ Conectar conta Google: canais que você segue e curtidas</button>`);
      box.querySelector('.vit-connect').addEventListener('click', async (e) => {
        const button = e.currentTarget; // depois do await o evento já não sabe quem foi clicado
        const r = await fetch('/api/google/connect', { method: 'POST' }).then((x) => x.json()).catch(() => null);
        button.textContent = r?.ok ? 'Termine no navegador e reabra esta aba.' : (r?.message || 'Não deu para abrir o login agora.');
      });
    }
    const add = (row, before = null) => {
      const section = document.createElement('div');
      section.className = 'vit-row';
      if (row.channels) { // canais seguidos: foto redonda; clique abre o canal no navegador
        section.innerHTML = `<div class="mus-section-label">${esc(row.title)}</div><div class="vit-chans">${row.channels.map((c) => `<button type="button" class="vit-chan" data-id="${esc(c.channelId)}"><img src="${esc(c.thumbnail)}" alt="" loading="lazy"><span>${esc(c.title)}</span></button>`).join('')}</div>`;
        section.querySelectorAll('.vit-chan').forEach((b) => b.addEventListener('click', () => fetch('/api/ui/open-external', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: `https://www.youtube.com/channel/${b.dataset.id}` }) })));
        box.insertBefore(section, before);
        return;
      }
      section.innerHTML = `<div class="mus-section-label">${esc(row.title)}</div><div class="vit-cards">${row.items.map((it, i) => `<button type="button" class="vit-card${row.video ? ' is-video' : ''}" data-i="${i}">
        <img src="${esc(it.thumbnail)}" alt="" loading="lazy"><b>${esc(it.title)}</b><span>${esc(it.channel)}</span></button>`).join('')}</div>`;
      section.querySelectorAll('.vit-card').forEach((card) => card.addEventListener('click', () => {
        const i = Number(card.dataset.i), it = row.items[i];
        if (!row.video || !globalThis.KiraDispatch) return globalThis.musicPlayItem?.(it, row.items, i);
        globalThis.closeDrawers?.(); // vídeo de verdade: abre no painel de vídeo da Kira (ele aceita até 6)
        globalThis.KiraDispatch({ type: 'open_panel', kind: 'video', id: 'vitrine-video', title: it.title, props: { state: 'play', items: row.items.slice(i, i + 6).map((x) => ({ videoId: x.videoId, title: x.title, thumbnail: x.thumbnail, url: x.watchUrl })) } });
      }));
      box.insertBefore(section, before);
    };
    const rest = await rows(playlists);
    rest.forEach((row) => { row.video = /vídeo/i.test(row.title); });
    [...mine, ...rest].forEach((row) => add(row));
    // Caminho B (Chrome da Kira logado no YouTube): recomendações e histórico, que o login oficial não entrega
    const firstRow = box.querySelector('.vit-row');
    json('/api/youtube/feed').then((feed) => {
      (feed?.rows || []).forEach((row) => add(row, firstRow));
      if (feed && !feed.logged) {
        box.insertAdjacentHTML('beforeend', '<button type="button" class="vit-connect vit-yt">▶ Entrar no YouTube: recomendações e histórico</button>');
        box.querySelector('.vit-yt').addEventListener('click', (e) => {
          const button = e.currentTarget;
          fetch('/api/youtube/browser-login', { method: 'POST' }).catch(() => {});
          button.textContent = 'Entre no YouTube no Chrome que abriu, feche ele e reabra esta aba.';
        });
      }
    });
  }

  // Playlist com capa: mosaico das capas das músicas dela (até 4)
  function covers() {
    document.querySelectorAll('#music-playlists-list .music-playlist-item').forEach((item) => {
      const wrap = item.querySelector('.mus-pl-icon-wrap');
      const urls = (item.__thumbs || []);
      if (!wrap || !urls.length || wrap.classList.contains('has-cover')) return;
      wrap.classList.add('has-cover');
      wrap.style.backgroundImage = (urls.length < 4 ? [urls[0]] : urls.slice(0, 4)).map((u) => `url("${u}")`).join(',');
      if (urls.length >= 4) wrap.classList.add('is-mosaic');
    });
  }

  async function paintCovers() {
    const playlists = (await json('/api/music/playlists'))?.data?.playlists || [];
    document.querySelectorAll('#music-playlists-list .music-playlist-item').forEach((item) => {
      const pl = playlists.find((p) => String(p.id || p.name) === item.dataset.plId);
      item.__thumbs = (pl?.items || []).map((x) => x.thumbnail).filter(Boolean);
    });
    covers();
  }

  const list = () => document.getElementById('music-playlists-list');
  const start = () => {
    render();
    const drawer = document.getElementById('music-drawer');
    if (drawer) new MutationObserver(() => { if (drawer.getAttribute('aria-hidden') === 'false') render(); }).observe(drawer, { attributes: true, attributeFilter: ['aria-hidden'] });
    if (list()) new MutationObserver(paintCovers).observe(list(), { childList: true });
    paintCovers();
  };
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', start) : start();
})();
