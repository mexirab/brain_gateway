// The dashboard password (DASHBOARD_TOKEN) and the session cookie derived from it.
// Web Crypto only (no Node imports), so the edge middleware, route handlers and
// server components can all share one check.

export const COOKIE_NAME = 'brain_token';

// Values that must never unlock the dashboard. `changeme` was the old code and
// compose default; an install that never set a real token would otherwise
// open the dashboard — and through /api/proxy the orchestrator — to anyone
// who guesses it.
const PLACEHOLDER_TOKENS = new Set(['', 'changeme']);

// The cookie holds HMAC(password, this label) — never the password itself, so a
// copied cookie or a cookie in a log doesn't reveal it. Changing DASHBOARD_TOKEN
// (or bumping the label) logs every browser out.
const SESSION_LABEL = 'brain-gateway-dashboard-session-v1';

/** The configured dashboard token, or null when unset/placeholder (fail closed). */
export function expectedDashboardToken(): string | null {
  const token = (process.env.DASHBOARD_TOKEN ?? '').trim();
  return PLACEHOLDER_TOKENS.has(token) ? null : token;
}

async function hmacHex(key: string, message: string): Promise<string> {
  const enc = new TextEncoder();
  const cryptoKey = await crypto.subtle.importKey('raw', enc.encode(key), { name: 'HMAC', hash: 'SHA-256' }, false, [
    'sign',
  ]);
  const sig = new Uint8Array(await crypto.subtle.sign('HMAC', cryptoKey, enc.encode(message)));
  return Array.from(sig, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** Compare without exiting early on the first differing character. */
export function timingSafeEqual(a: string, b: string): boolean {
  const len = Math.max(a.length, b.length);
  let diff = a.length ^ b.length;
  for (let i = 0; i < len; i++) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

/** The cookie value for the configured token, or null when login is disabled. */
export async function expectedSessionValue(): Promise<string | null> {
  const token = expectedDashboardToken();
  return token === null ? null : hmacHex(token, SESSION_LABEL);
}

export async function isValidDashboardSession(cookie: string | null | undefined): Promise<boolean> {
  const expected = await expectedSessionValue();
  return expected !== null && typeof cookie === 'string' && timingSafeEqual(cookie, expected);
}

/** Password check for the login route: both sides hashed first, so the compare
 * is over equal-length strings and leaks nothing about the password's length. */
export async function isCorrectPassword(candidate: unknown): Promise<boolean> {
  const expected = await expectedSessionValue();
  if (expected === null || typeof candidate !== 'string') return false;
  return timingSafeEqual(await hmacHex(candidate, SESSION_LABEL), expected);
}
