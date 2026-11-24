// What a kiosk session (lib/dashboard-auth.ts → 'kiosk') may do: open /dashboard, and make exactly
// the GET proxy calls that page makes. Everything else is 403: every other page (middleware.ts),
// every other proxy path, any query string the page doesn't send, and every write (route.ts).
//
// Allow-list, not deny-list: a new page or orchestrator route is closed to the kiosk until it is
// added here. KIOSK_PROXY_GETS is the dashboard page's call list; kiosk-session.test.tsx renders
// app/(private)/dashboard/page.tsx and fails when the page asks for anything else, so a new card
// has to be added here on purpose (and only if a read-only screen in the closet may show it).

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

/**
 * May a kiosk session make this proxy call? `segments` are the catch-all's (already decoded)
 * path segments and `search` the raw query, both exactly as route.ts forwards them. Empty
 * segments and segments holding a `/` or `\` (a decoded %2F) are refused, so the joined string is
 * the path the orchestrator would really get.
 */
export function kioskMayProxy(method: string, segments: readonly string[], search: string): boolean {
  if (method !== 'GET') return false;
  if (segments.length === 0 || segments.some((s) => s === '' || s.includes('/') || s.includes('\\'))) return false;
  return ALLOWED.has('/' + segments.join('/') + search);
}
