'use strict';
self.addEventListener('push', event => {
  event.waitUntil(self.registration.showNotification('BinFlow · Lunch time', {
    body: 'Four hours on shift. Park safely, then start your 30-minute lunch.',
    icon: '/static/binflow-touch.png', tag: 'binflow-lunch', renotify: true, silent: false
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(self.clients.openWindow('/binflow/driver/my-day'));
});
