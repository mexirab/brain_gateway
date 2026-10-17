'use client';

import { useEffect } from 'react';
import { safeRedirect } from '@/lib/protected-paths';

// When the dashboard session ends (password changed, cookie format changed on a
// deploy, cookie expired), every /api/proxy call answers 401 and widgets just
// show errors. Send the page to /login instead — once — and come back after.
export default function SessionExpiredWatcher() {
  useEffect(() => {
    const original = window.fetch;
    let redirecting = false;
    window.fetch = async (input, init) => {
      const res = await original(input, init);
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      const path = url.startsWith('http') ? new URL(url).pathname : url;
      if (res.status === 401 && path.startsWith('/api/proxy/') && !redirecting) {
        redirecting = true;
        const here = safeRedirect(window.location.pathname + window.location.search);
        window.location.assign(`/login?redirect=${encodeURIComponent(here)}`);
      }
      return res;
    };
    return () => {
      window.fetch = original;
    };
  }, []);
  return null;
}
