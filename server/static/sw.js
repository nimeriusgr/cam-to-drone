/* SKYWARDEN · service worker: shows push notifications and opens the incident when tapped */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || 'SKYWARDEN', {
    body: d.body || '', tag: d.tag || 'skywarden', renotify: true, requireInteraction: !!d.urgent,
    icon: '/static/icon-192.png', badge: '/static/badge-96.png', vibrate: d.urgent ? [250, 120, 250, 120, 500] : [120],
    data: { url: d.url || '/' },
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || '/', self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) {
      if (new URL(c.url).origin === self.location.origin) { c.postMessage({ open: url }); return c.focus(); }
    }
    return self.clients.openWindow(url);
  }));
});
