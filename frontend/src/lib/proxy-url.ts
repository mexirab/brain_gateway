// Upstream URL for the /api/proxy/[...path] route, which forwards to the
// orchestrator with the API_TOKEN bearer attached.
//
// Next hands the catch-all segments over already percent-decoded, so a request
// for `/api/proxy/%2Fevil.host/x` arrives as ['/evil.host', 'x']. Joining those
// raw into a path gives `//evil.host/x`, which `new URL(path, base)` resolves
// as a different host — and the bearer goes with it. So every segment is
// re-encoded, separators and dot-segments are refused, and the result must
// keep the orchestrator's origin.

// Every orchestrator path the dashboard calls lives under one of these.
const ALLOWED_ROOTS = new Set(['api', 'v1', 'health']);

// eslint-disable-next-line no-control-regex
const UNSAFE_SEGMENT = /[/\\\u0000-\u001f\u007f]/;

/** The orchestrator URL for these path segments, or null if they're not allowed. */
export function buildUpstreamUrl(segments: string[], search: string, base: string): URL | null {
  if (segments.length === 0 || !ALLOWED_ROOTS.has(segments[0])) return null;
  if (segments.some((s) => s === '' || s === '.' || s === '..' || UNSAFE_SEGMENT.test(s))) return null;

  const origin = new URL(base).origin;
  let url: URL;
  try {
    url = new URL('/' + segments.map(encodeURIComponent).join('/'), origin);
  } catch {
    return null; // encodeURIComponent throws on a lone surrogate
  }
  url.search = search;
  return url.origin === origin ? url : null;
}
