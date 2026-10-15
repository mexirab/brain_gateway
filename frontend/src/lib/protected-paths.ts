// Routes that require the dashboard cookie. Shared by middleware.ts (the auth
// gate) and the login page (which only returns the user to one of these).
//
// `/architecture` is included deliberately: it shows live infra status via the
// authenticated `/api/services` + `/health` proxy. Every route under
// app/(private)/ must be listed here AND in middleware.ts `config.matcher`
// (Next needs the matcher to be a static literal) — middleware.test.ts
// enforces both.
export const PROTECTED_PATHS = ['/dashboard', '/chat', '/tasks', '/home', '/finance', '/announcements', '/shopping', '/documents', '/architecture', '/settings', '/workouts', '/meals', '/pantry', '/personal-facts'];

export const DEFAULT_LOGIN_REDIRECT = '/dashboard';

export function isProtectedPath(pathname: string): boolean {
  return PROTECTED_PATHS.some((p) => pathname === p || pathname.startsWith(p + '/'));
}

// Placeholder origin for resolving a relative redirect; never navigated to.
const BASE = 'http://login.invalid';

/**
 * Turn the login page's `?redirect=` value into a same-origin app path.
 *
 * The value is attacker-controllable (anyone can send a /login link), so it is
 * resolved with the WHATWG URL parser — the same rules the browser applies —
 * rather than prefix-checked: that catches `//host`, `/\host`, tab/newline
 * smuggling and `javascript:`/`data:` schemes. Anything that leaves the origin,
 * isn't a protected app route, or keeps an encoded slash/backslash in the path
 * (`..%2F..` survives parsing as one segment; Next 404s it today, but don't
 * rely on that) falls back to the dashboard. The returned value is rebuilt
 * from the parsed URL, never the raw input.
 */
export function safeRedirect(raw: string | null | undefined): string {
  if (!raw || !raw.startsWith('/')) return DEFAULT_LOGIN_REDIRECT;
  let url: URL;
  try {
    url = new URL(raw, BASE);
  } catch {
    return DEFAULT_LOGIN_REDIRECT;
  }
  if (url.origin !== BASE || !isProtectedPath(url.pathname) || /%2f|%5c/i.test(url.pathname)) {
    return DEFAULT_LOGIN_REDIRECT;
  }
  return url.pathname + url.search + url.hash;
}
