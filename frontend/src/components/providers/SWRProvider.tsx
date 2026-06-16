'use client';

import { ReactNode } from 'react';
import { SWRConfig } from 'swr';

/**
 * App-wide SWR defaults for the dashboard data layer.
 *
 * - `revalidateOnFocus` / `revalidateOnReconnect`: refresh stale data when the
 *   user returns to the tab or the network comes back — replaces the manual
 *   re-fetch-on-mount each card used to do.
 * - `dedupingInterval`: collapse duplicate requests for the same key fired
 *   within 5s (e.g. several cards mounting at once, or focus + interval racing).
 * - `errorRetryCount`: bounded retry with SWR's exponential backoff instead of
 *   the old "fail once and sit there" behaviour.
 *
 * Note on the Callisto Pi kiosk: SWR's `refreshInterval` (set per-hook) is
 * automatically PAUSED while the tab is hidden (`refreshWhenHidden` defaults
 * false), so polling cards stop hammering the orchestrator when the kiosk
 * display sleeps — a key reason for adopting SWR here.
 */
export function SWRProvider({ children }: { children: ReactNode }) {
  return (
    <SWRConfig
      value={{
        revalidateOnFocus: true,
        revalidateOnReconnect: true,
        dedupingInterval: 5000,
        errorRetryCount: 3,
        keepPreviousData: true,
      }}
    >
      {children}
    </SWRConfig>
  );
}
