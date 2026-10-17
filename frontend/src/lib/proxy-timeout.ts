/**
 * How long the dashboard proxy waits for the orchestrator to START answering
 * (response headers). Once headers arrive the timer is cleared, so a long SSE
 * chat stream or a big download is never cut off mid-body.
 *
 * Chat and a few slow endpoints get longer budgets; everything else is a
 * quick API call, where a hung handler should come back as a clear 504
 * instead of tying up the request forever. A slow budget must stay above the
 * orchestrator's own inner timeout, or the user sees a 504 for work that then
 * finishes anyway (and a retry does it twice, e.g. a duplicate meal log).
 */
export const DEFAULT_UPSTREAM_TIMEOUT_MS = 120_000;
/** Inner budgets of 120 s plus upload/apply time. */
export const SLOW_UPSTREAM_TIMEOUT_MS = 300_000;
/** A non-streaming reply sends headers only after the whole tool loop
 *  (up to 5 model rounds of 120 s each on the single llama slot). */
export const CHAT_UPSTREAM_TIMEOUT_MS = 600_000;

// Joined path → why it is slow.
const SLOW_PATHS = new Set([
  'api/meals/photo', // vision estimate, VISION_TIMEOUT 120 s behind chat
  'api/finance/sync', // Actual snapshot, 120 s executor timeout + apply
  'api/finance/categories', // same Actual snapshot
  'api/self_audit/run', // Loki queries + one LLM call
  'api/paperless/upload', // up to 100 MB upload, then Paperless
]);

export function upstreamTimeoutMs(path: string[]): number {
  const joined = path.join('/');
  if (joined === 'v1/chat/completions') return CHAT_UPSTREAM_TIMEOUT_MS;
  if (SLOW_PATHS.has(joined)) return SLOW_UPSTREAM_TIMEOUT_MS;
  return DEFAULT_UPSTREAM_TIMEOUT_MS;
}
