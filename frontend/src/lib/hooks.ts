'use client';

import useSWR, { type SWRConfiguration } from 'swr';
import { api } from './api';
import { financeApi } from './finance-api';

/**
 * Typed SWR hooks over `lib/api`. These replace the per-component
 * `useState + useEffect + setInterval` polling boilerplate with a shared cache
 * that dedupes, revalidates on focus/reconnect, and (for polling hooks) pauses
 * automatically when the tab is hidden. See `SWRProvider` for global defaults.
 *
 * Polling hooks whose components previously used `setInterval` keep that exact
 * cadence. `useReminders`/`useCalendarToday` were one-shot before and stay so
 * (they have an inline ErrorState Retry for manual recovery + focus/reconnect
 * revalidation). The two LINK-cards — `useHealth` (SystemHealthCard) and
 * `useFinanceSnapshot` (FinanceSnapshotCard) — can't render an inline Retry
 * button (it would nest a <button> inside their <a>/<Link>), so they get a
 * gentle poll instead, which lets a transient failure self-recover on the
 * always-on kiosk without user interaction.
 */

// Poll cadences (ms) — mirror the intervals the components used pre-SWR.
const POLL = {
  health: 30_000,
  focus: 15_000,
  progress: 60_000,
  announcementsCard: 30_000,
  announcementsPage: 15_000,
  temperature: 60_000,
  selfcare: 30_000,
  shopping: 10_000,
  financeSnapshot: 60_000,
} as const;

// ---- Dashboard cards -------------------------------------------------------

export function useHealth(config?: SWRConfiguration) {
  return useSWR('health', () => api.health(), { refreshInterval: POLL.health, ...config });
}

/**
 * Per-service health from the orchestrator (`GET /api/services`). Drives the
 * LIVE node status on the architecture page. Same cadence as `useHealth`.
 */
export function useServices(config?: SWRConfiguration) {
  return useSWR('services', () => api.services(), {
    refreshInterval: POLL.health,
    ...config,
  });
}

export function useReminders(config?: SWRConfiguration) {
  return useSWR('reminders', () => api.reminders(), config);
}

export function useCalendarToday(config?: SWRConfiguration) {
  return useSWR('calendar:today', () => api.calendarToday(), config);
}

export function useFocus(config?: SWRConfiguration) {
  return useSWR('focus', () => api.focus(), { refreshInterval: POLL.focus, ...config });
}

export function useProgress(config?: SWRConfiguration) {
  return useSWR(
    'progress',
    async () => {
      const [today, week, streaks] = await Promise.all([
        api.progressToday(),
        api.progressWeek(),
        api.progressStreaks(),
      ]);
      return { today, week, streaks };
    },
    { refreshInterval: POLL.progress, ...config },
  );
}

export function useTemperatures(config?: SWRConfiguration) {
  return useSWR('temperatures', () => api.temperatures(), {
    refreshInterval: POLL.temperature,
    ...config,
  });
}

export function useSelfcareToday(config?: SWRConfiguration) {
  return useSWR('selfcare:today', () => api.selfcareToday(), {
    refreshInterval: POLL.selfcare,
    ...config,
  });
}

export function useFinanceSnapshot(config?: SWRConfiguration) {
  return useSWR(
    'finance:snapshot',
    async () => {
      const [budget, game] = await Promise.all([
        financeApi.getCurrentBudget(),
        financeApi.getGameState(),
      ]);
      return { budget, game };
    },
    { refreshInterval: POLL.financeSnapshot, ...config },
  );
}

// ---- Announcements (card uses a small limit + slow poll; page uses a big
//      limit + faster poll, so they're distinct cache keys) -----------------

export function useAnnouncements(
  limit: number,
  refreshInterval: number,
  config?: SWRConfiguration,
) {
  return useSWR(
    ['announcements', limit] as const,
    async () => {
      const [history, stats] = await Promise.all([
        api.announcementHistory(limit),
        api.announcementStats(),
      ]);
      return { history, stats };
    },
    { refreshInterval, ...config },
  );
}

export const useAnnouncementsCard = (config?: SWRConfiguration) =>
  useAnnouncements(15, POLL.announcementsCard, config);

export const useAnnouncementsPage = (config?: SWRConfiguration) =>
  useAnnouncements(200, POLL.announcementsPage, config);

// ---- Shopping --------------------------------------------------------------

export function useShopping(showChecked: boolean, config?: SWRConfiguration) {
  return useSWR(
    ['shopping', showChecked] as const,
    () => api.shoppingList(undefined, showChecked),
    { refreshInterval: POLL.shopping, ...config },
  );
}
