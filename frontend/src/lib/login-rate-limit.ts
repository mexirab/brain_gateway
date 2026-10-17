// Brute-force brake for POST /api/auth/login (in-memory: the frontend is one
// Node process). Per client: 5 failures in 15 min locks that client out until
// the window passes. Overall: 30 failures in 15 min locks everyone out — the
// client key comes from X-Forwarded-For, which a LAN client talking to :3001
// directly can forge, so the global cap is the real ceiling. A lockout never
// touches existing sessions; it only blocks new logins.

const WINDOW_MS = 15 * 60 * 1000;
export const MAX_FAILURES_PER_CLIENT = 5;
export const MAX_FAILURES_TOTAL = 30;
const MAX_TRACKED = 1000;

type Entry = { count: number; first: number };
const perClient = new Map<string, Entry>();
let total: Entry = { count: 0, first: 0 };

function fresh(e: Entry | undefined, now: number): Entry {
  return e && now - e.first < WINDOW_MS ? e : { count: 0, first: now };
}

/** Client key: the LAST X-Forwarded-For hop — Tailscale Serve appends the real
 * peer to whatever the client sent, so earlier hops are client-chosen. Direct
 * :3001 requests have no trusted proxy at all; the global cap covers those. */
export function clientKey(headers: Headers): string {
  const hops = (headers.get('x-forwarded-for') ?? '').split(',').map((h) => h.trim()).filter(Boolean);
  return (hops[hops.length - 1] || headers.get('x-real-ip') || 'direct').slice(0, 64);
}

/** Seconds until this client may try again, or 0 when allowed. */
export function retryAfterSeconds(key: string, now = Date.now()): number {
  const t = fresh(total, now);
  const c = fresh(perClient.get(key), now);
  const blocked = [t.count >= MAX_FAILURES_TOTAL ? t : null, c.count >= MAX_FAILURES_PER_CLIENT ? c : null].filter(
    (e): e is Entry => e !== null,
  );
  if (!blocked.length) return 0;
  return Math.max(1, Math.ceil(Math.max(...blocked.map((e) => e.first + WINDOW_MS - now)) / 1000));
}

/**
 * Reserve one attempt before any await: returns seconds to wait (0 = go ahead,
 * and the attempt is already counted as a failure). Checking first and
 * counting after the slow password hash let a burst of parallel guesses all
 * pass the check. Call recordSuccess() when the password turns out right.
 */
export function beginAttempt(key: string, now = Date.now()): number {
  const wait = retryAfterSeconds(key, now);
  if (wait === 0) recordFailure(key, now);
  return wait;
}

export function recordFailure(key: string, now = Date.now()): void {
  total = fresh(total, now);
  total.count++;
  const c = fresh(perClient.get(key), now);
  c.count++;
  perClient.delete(key);
  if (perClient.size >= MAX_TRACKED) {
    const oldest = perClient.keys().next().value;
    if (oldest !== undefined) perClient.delete(oldest);
  }
  perClient.set(key, c);
}

export function recordSuccess(key: string): void {
  perClient.delete(key);
  if (total.count > 0) total.count--; // give back the slot beginAttempt reserved
}

/** Tests only. */
export function _resetLoginRateLimit(): void {
  perClient.clear();
  total = { count: 0, first: 0 };
}
