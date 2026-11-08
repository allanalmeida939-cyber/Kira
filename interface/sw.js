// App da Kira no celular (instalado pelo Chrome): mostra as notificações que a Kira manda (services/web_push.py),
// mesmo com o app fechado, e abre o app ao tocar nelas. Sem cache: tudo vem do PC na hora; sem o PC, uma página simples.

const OFFLINE = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
  + '<title>Kira</title><body style="margin:0;display:grid;place-items:center;height:100vh;background:#07040f;color:#ddd6fe;'
  + 'font-family:system-ui;text-align:center"><div><h2>Kira fora do ar</h2><p>O PC da Kira está desligado ou sem internet.</p>'
  + '<p><a href="/" style="color:#c4b5fd">Tentar de novo</a></p></div>';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

// só a navegação passa por aqui (o resto vai direto para a rede): sem o PC, a página de "fora do ar"
self.addEventListener('fetch', (event) => {
  if (event.request.mode !== 'navigate') return;
  event.respondWith(fetch(event.request).catch(() => new Response(OFFLINE, { headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
});

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = { body: event.data ? event.data.text() : '' }; }
  // Título sempre "Kira" (o assunto vai na frente do texto). O endereço do site que o Chrome mostra no topo da
  // notificação não dá para tirar pelo código: some quando a Kira é instalada como app (Adicionar à tela inicial).
  const topic = String(data.title || '').trim();
  const body = String(data.body || '');
  event.waitUntil(self.registration.showNotification('Kira', {
    body: ((topic && topic !== 'Kira' ? `${topic}: ` : '') + body).slice(0, 300),
    icon: '/assets/ui/app/kira-192.png',
    badge: '/assets/ui/app/kira-badge-96.png', // monocromático: colorido o Android pinta como quadrado branco
    timestamp: Date.now(),
    tag: data.tag || undefined,
    renotify: Boolean(data.tag),
    vibrate: [120, 60, 120],
    data: { url: typeof data.url === 'string' && data.url.startsWith('/') ? data.url : '/' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const app = open.find((client) => new URL(client.url).origin === self.location.origin);
    if (app) return app.focus();
    return self.clients.openWindow(url);
  })());
});
