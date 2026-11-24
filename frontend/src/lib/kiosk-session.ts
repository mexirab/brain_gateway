// What a kiosk session (lib/dashboard-auth.ts → 'kiosk') may do: open /dashboard, and make exactly
// the GET proxy calls that page makes. Everything else is 403: every other page (middleware.ts),
// every other proxy path, any query string the page doesn't send, and every write (route.ts).
//
// Allow-list, not deny-list: a new page or orchestrator route is closed to the kiosk until it is
// added here. KIOSK_PROXY_GETS is the dashboard page's call list; kiosk-session.test.tsx renders
// app/(private)/dashboard/page.tsx and fails when the page asks for anything else, so a new card
// has to be added here on purpose (and only if a read-only screen in the closet may show it).

import { logEvent } from './server-log';

export const KIOSK_PAGES: readonly string[] = ['/dashboard'];

// Path + exact query, as lib/api.ts and lib/finance-api.ts build them for the dashboard cards.
export const KIOSK_PROXY_GETS: readonly string[] = [
  '/health', // SystemHealthCard
  '/api/calendar/today', // CalendarCard
  '/api/tasks?status=open', // TasksCard
  '/api/reminders', // RemindersCard
  '/api/selfcare/today', // SelfcareTodayCard
  '/api/focus', // FocusTimerCard
  '/api/progress/today', // ProgressCard
  '/api/progress/week',
  '/api/progress/streaks',
  '/api/announcements/history?limit=15', // AnnouncementHistoryCard
  '/api/announcements/stats',
  '/api/temperatures', // TemperatureCard
  '/api/finance/budget/current', // FinanceSnapshotCard
  '/api/finance/game-state',
];

const ALLOWED = new Set(KIOSK_PROXY_GETS);

/** May a kiosk session open this page? Exact paths only (no sub-pages). */
export function kioskMayOpenPage(pathname: string): boolean {
  return KIOSK_PAGES.includes(pathname);
}

// In a decoded segment these mean an encoded separator or query (`%2F`, `%3F`, …): the joined string
// would then differ from the URL the orchestrator gets, so they never match.
const UNSAFE_SEGMENT = /[/\\?#%]/;

/**
 * May a kiosk session make this proxy call? `segments` are the catch-all's (already decoded)
 * path segments and `search` the raw query, both exactly as route.ts forwards them. Empty segments
 * and segments holding UNSAFE_SEGMENT characters are refused, so the joined string is the path the
 * orchestrator would really get.
 */
export function kioskMayProxy(method: string, segments: readonly string[], search: string): boolean {
  if (method !== 'GET') return false;
  if (segments.length === 0 || segments.some((s) => s === '' || UNSAFE_SEGMENT.test(s))) return false;
  return ALLOWED.has('/' + segments.join('/') + search);
}

// Refusal log: one warning per (method, route), then at most one per REFUSAL_LOG_MS carrying how many
// were left out, so a card that drifted from the allow-list (polling every 15 s) can't flood the logs
// the self-audit reads. The kiosk picks the path, so only plain segment names are logged.
const REFUSAL_LOG_MS = 10 * 60_000;
const MAX_REFUSAL_KEYS = 200;
const refusals = new Map<string, { at: number; suppressed: number }>();

function refusalLabel(segments: readonly string[]): string {
  return segments
    .slice(0, 2)
    .map((s) => (/^[a-z0-9_.-]{1,32}$/i.test(s) ? s : '?'))
    .join('/');
}

export function logKioskRefusal(method: string, segments: readonly string[], now = Date.now()): void {
  const route = refusalLabel(segments);
  const key = `${method} ${route}`;
  const seen = refusals.get(key);
  if (seen && now - seen.at < REFUSAL_LOG_MS) {
    seen.suppressed += 1;
    return;
  }
  if (refusals.size >= MAX_REFUSAL_KEYS) refusals.clear(); // bounded: the kiosk chooses the keys
  refusals.set(key, { at: now, suppressed: 0 });
  logEvent('warning', '[PROXY] Kiosk session refused', { route, method, suppressed: seen?.suppressed ?? 0 });
}

/** Tests only. */
export function _resetKioskRefusalLog(): void {
  refusals.clear();
}
