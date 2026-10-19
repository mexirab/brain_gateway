/*
 * Jess service worker — Web Push only.
 *
 * Deliberately has NO fetch handler and caches nothing: every dashboard page
 * and /api/proxy response is behind the session cookie, and a cached copy
 * could outlive a logout. All this does is show pushed notifications and
 * open /chat (where the Done / Snooze buttons live) when one is tapped.
 */

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

async function tellOpenTabs() {
  const tabs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const tab of tabs) tab.postMessage({ type: 'inbox-changed' });
  return tabs;
}

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : '' };
  }
  const title = typeof data.title === 'string' && data.title ? data.title : 'Jess';
  const options = {
    body: typeof data.body === 'string' ? data.body : '',
    tag: typeof data.tag === 'string' ? data.tag : 'jess',
    icon: '/icons/icon-192.png',
    // Only same-origin paths; the server always sends /chat.
    data: { url: typeof data.url === 'string' && data.url.startsWith('/') && !data.url.startsWith('//') ? data.url : '/chat' },
  };
  // iOS revokes the subscription if a push doesn't show a notification.
  event.waitUntil(Promise.all([self.registration.showNotification(title, options), tellOpenTabs()]));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/chat';
  event.waitUntil(
    (async () => {
      const tabs = await tellOpenTabs();
      const tab = tabs.find((t) => 'focus' in t);
      if (!tab) return self.clients.openWindow(url);
      // Focus first: browsers only allow it right after the tap, so it can't
      // wait for a navigation. Then ask the page to route to /chat (works
      // where WindowClient.navigate doesn't, e.g. older iOS).
      const focused = await tab.focus().catch(() => tab);
      focused.postMessage({ type: 'open', url });
      return focused;
    })(),
  );
});
