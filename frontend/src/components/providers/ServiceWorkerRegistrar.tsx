'use client';

import { useEffect } from 'react';
import { usePathname, useRouter } from 'next/navigation';

/**
 * Registers /sw.js (Web Push only — no caching; see public/sw.js). Needed
 * before Settings → Notifications can subscribe. Also routes a notification
 * tap: the worker focuses an open tab and posts {type:'open', url}, and this
 * (mounted on every private page) moves it to /chat. Renders nothing.
 */
export default function ServiceWorkerRegistrar() {
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!('serviceWorker' in navigator)) return;
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {
      /* unsupported / insecure context (plain-http LAN) — push just stays off */
    });
  }, []);

  useEffect(() => {
    if (!('serviceWorker' in navigator)) return;
    const onMessage = (e: MessageEvent) => {
      const url = e.data?.type === 'open' ? e.data.url : null;
      // Same-origin paths only (the worker already filters; belt and braces).
      if (typeof url === 'string' && url.startsWith('/') && !url.startsWith('//') && url !== pathname) {
        router.push(url);
      }
    };
    navigator.serviceWorker.addEventListener('message', onMessage);
    return () => navigator.serviceWorker.removeEventListener('message', onMessage);
  }, [router, pathname]);

  return null;
}
