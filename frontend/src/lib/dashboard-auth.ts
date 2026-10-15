// The dashboard password (DASHBOARD_TOKEN) and the cookie that carries it.
// No imports, so the edge middleware, route handlers and server components
// can all share one check.

export const COOKIE_NAME = 'brain_token';

// Values that must never unlock the dashboard. `changeme` was the old code and
// compose default; an install that never set a real token would otherwise
// open the dashboard — and through /api/proxy the orchestrator — to anyone
// who guesses it.
const PLACEHOLDER_TOKENS = new Set(['', 'changeme']);

/** The configured dashboard token, or null when unset/placeholder (fail closed). */
export function expectedDashboardToken(): string | null {
  const token = (process.env.DASHBOARD_TOKEN ?? '').trim();
  return PLACEHOLDER_TOKENS.has(token) ? null : token;
}

export function isValidDashboardToken(candidate: string | null | undefined): boolean {
  const expected = expectedDashboardToken();
  return expected !== null && candidate === expected;
}
