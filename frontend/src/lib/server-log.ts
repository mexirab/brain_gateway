// One JSON line per event, in the orchestrator's shape ({ts, level, logger,
// msg}), so promtail sets `level` and the daily self-audit sees warnings.
// Content-free by rule: never a password, cookie, query string, full path or
// body — only enums, counts, statuses and the first two path segments.

type Level = 'info' | 'warning' | 'error';
type Field = string | number | boolean | null;

export function logEvent(level: Level, msg: string, fields: Record<string, Field> = {}): void {
  const line = JSON.stringify({ ts: new Date().toISOString(), level, logger: 'frontend', msg, ...fields });
  if (level === 'error') console.error(line);
  else console.warn(line); // stdout/stderr both reach docker logs + Loki
}

/** "/api/documents/123/download" → "api/documents" (ids and names stay out of logs). */
export function routeLabel(path: string[]): string {
  return path.slice(0, 2).join('/').slice(0, 60);
}
