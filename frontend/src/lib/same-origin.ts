// CSRF guard for state-changing requests (proxy writes, login, logout).
//
// The session cookie is SameSite=Lax, but "same site" covers every port on
// Jupiter (Grafana, SearXNG, Actual, …) and every *.tail74fc4a.ts.net node, so
// a page served by any of them could otherwise POST through /api/proxy with the
// dashboard cookie attached. Browsers label every request with Sec-Fetch-Site;
// only "same-origin" (our own pages) or "none" (typed by the user) may write.
// Older browsers without it still send Origin on POST, which must match our host.
// A request with neither header isn't from a browser, so it has no cookie to abuse.

function requestHost(headers: Headers): string | null {
  return (headers.get('x-forwarded-host') || headers.get('host'))?.split(',')[0].trim().toLowerCase() || null;
}

export function isCrossSiteRequest(headers: Headers): boolean {
  const site = headers.get('sec-fetch-site');
  if (site !== null) return site !== 'same-origin' && site !== 'none';
  const origin = headers.get('origin');
  if (origin === null) return false;
  if (origin === 'null') return true; // sandboxed/opaque origins
  try {
    return new URL(origin).host.toLowerCase() !== requestHost(headers);
  } catch {
    return true;
  }
}
