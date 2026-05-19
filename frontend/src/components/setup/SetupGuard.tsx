'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { setupApi } from '@/lib/setup-api';

/**
 * First-boot redirect. Rendered (invisibly) inside the private layout: on
 * mount it checks `GET /api/setup/status` and, if the setup wizard has not
 * been completed, sends the user to `/setup`.
 *
 * Failure is non-fatal — if the status check errors we do NOT redirect, so a
 * transient orchestrator hiccup can't lock the user out of their dashboard.
 */
export default function SetupGuard() {
  const router = useRouter();

  useEffect(() => {
    let cancelled = false;
    setupApi
      .getStatus()
      .then((s) => {
        if (!cancelled && !s.setup_completed) router.replace('/setup');
      })
      .catch(() => {
        // non-fatal — leave the user where they are
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  return null;
}
