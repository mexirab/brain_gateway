// The dashboard password (DASHBOARD_TOKEN), the optional kiosk token (KIOSK_TOKEN) and the session
// cookie derived from them. Web Crypto only (no Node imports), so the edge middleware, route
// handlers and server components can all share one check.
//
// Two session kinds share the one cookie:
//   - 'full'  = HMAC(DASHBOARD_TOKEN, SESSION_LABEL): what /login sets; opens everything.
//   - 'kiosk' = HMAC(KIOSK_TOKEN, KIOSK_SESSION_LABEL): read-only, for unattended screens (the
//     closet kiosk, scripts/jupiter-kiosk). Never set by /login; the kiosk sets it itself. It may
//     open /dashboard and the GET calls that page makes, nothing else (lib/kiosk-session.ts).

import { logEvent } from './server-log';

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

// Kiosk sessions: their own key and label, so a kiosk cookie is never a full session (even if the
// two tokens were equal) and the kiosk can be logged out by changing KIOSK_TOKEN alone, without
// logging the phone out. scripts/jupiter-kiosk/make_logins.py repeats the label and the length rule.
const KIOSK_SESSION_LABEL = 'brain-gateway-kiosk-session-v1';
export const MIN_KIOSK_TOKEN_LENGTH = 24;

export type SessionKind = 'full' | 'kiosk';

/** The configured dashboard token, or null when unset/placeholder (fail closed). */
export function expectedDashboardToken(): string | null {
  const token = (process.env.DASHBOARD_TOKEN ?? '').trim();
  return PLACEHOLDER_TOKENS.has(token) ? null : token;
}

/**
 * The configured kiosk token, or null when kiosk sessions are off: KIOSK_TOKEN unset, a
 * placeholder, shorter than MIN_KIOSK_TOKEN_LENGTH, equal to DASHBOARD_TOKEN (then changing it
 * would log the phone out too), or the dashboard has no login at all.
 */
export function expectedKioskToken(): string | null {
  const token = (process.env.KIOSK_TOKEN ?? '').trim();
  if (PLACEHOLDER_TOKENS.has(token)) return null; // not configured: kiosk sessions are simply off
  const dashboard = expectedDashboardToken();
  if (dashboard === null) return kioskTokenIgnored('no_dashboard_token');
  if (token.length < MIN_KIOSK_TOKEN_LENGTH) return kioskTokenIgnored('too_short');
  if (token === dashboard) return kioskTokenIgnored('same_as_dashboard');
  return token;
}

// A KIOSK_TOKEN that is set but ignored logs the kiosk out with nothing else to show for it (its own
// journal isn't shipped to Loki), so say so once per process. Reason only, never the value.
let kioskTokenWarned = false;
function kioskTokenIgnored(reason: 'no_dashboard_token' | 'too_short' | 'same_as_dashboard'): null {
  if (!kioskTokenWarned) {
    kioskTokenWarned = true;
    logEvent('warning', '[AUTH] KIOSK_TOKEN is set but ignored: kiosk sessions are off', { reason });
  }
  return null;
}

/** Tests only. */
export function _resetKioskTokenWarning(): void {
  kioskTokenWarned = false;
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

/** The full-session cookie value for the configured token, or null when login is disabled. */
export async function expectedSessionValue(): Promise<string | null> {
  const token = expectedDashboardToken();
  return token === null ? null : hmacHex(token, SESSION_LABEL);
}

/** The kiosk-session cookie value, or null when kiosk sessions are off. */
export async function expectedKioskSessionValue(): Promise<string | null> {
  const token = expectedKioskToken();
  return token === null ? null : hmacHex(token, KIOSK_SESSION_LABEL);
}

/** Which session the cookie is: 'full', 'kiosk', or null (none / wrong / login disabled). */
export async function dashboardSessionKind(cookie: string | null | undefined): Promise<SessionKind | null> {
  if (typeof cookie !== 'string') return null;
  const [full, kiosk] = await Promise.all([expectedSessionValue(), expectedKioskSessionValue()]);
  // Both compares always run, so the answer time doesn't say which kind was tried.
  const isFull = full !== null && timingSafeEqual(cookie, full);
  const isKiosk = kiosk !== null && timingSafeEqual(cookie, kiosk);
  if (isFull) return 'full';
  return isKiosk ? 'kiosk' : null;
}

/** True only for a FULL session. Kiosk sessions are allowed explicitly, never by default. */
export async function isValidDashboardSession(cookie: string | null | undefined): Promise<boolean> {
  return (await dashboardSessionKind(cookie)) === 'full';
}

/** Password check for the login route: both sides hashed first, so the compare
 * is over equal-length strings and leaks nothing about the password's length.
 * Only DASHBOARD_TOKEN logs in; KIOSK_TOKEN is not a password. */
export async function isCorrectPassword(candidate: unknown): Promise<boolean> {
  const expected = await expectedSessionValue();
  // '' too: Web Crypto refuses a zero-length HMAC key, which was a 500 instead of a 401.
  if (expected === null || typeof candidate !== 'string' || candidate === '') return false;
  return timingSafeEqual(await hmacHex(candidate, SESSION_LABEL), expected);
}
