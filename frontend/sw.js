/* Service Worker — Family Radio PWA */

self.addEventListener('install', (e) => {
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(self.clients.claim());
});

self.addEventListener('push', (e) => {
  var data = {};
  try {
    data = e.data ? e.data.json() : {};
  } catch (_) {}

  var title = data.title || 'Family Radio';
  var options = {
    body: data.body || '',
    icon: '/static/icon-192.png',
    badge: '/static/badge-96.png',
    tag: 'new-track',
    renotify: true,
    data: { url: data.url || '/#playing' },
  };

  e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  var target = (e.notification.data && e.notification.data.url) || '/#playing';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (var i = 0; i < clients.length; i++) {
        var c = clients[i];
        if ('focus' in c) {
          c.postMessage({ type: 'navigate', url: target });
          return c.focus();
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});
