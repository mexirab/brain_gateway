import { PRIMARY_MODEL_FALLBACK } from './constants';

/**
 * Extract a human-friendly primary-model name from the orchestrator `/health`
 * `primary` field, which is formatted as `"<url> (<model name>)"`
 * (see orchestrator/api_routes.py). Falls back to a generic label when the
 * field is missing (anonymous render, orchestrator down) so we never show a
 * guessed/stale model string.
 */
export function parsePrimaryModel(primary: string | undefined): string {
  if (!primary) return PRIMARY_MODEL_FALLBACK;
  const match = primary.match(/\(([^)]+)\)\s*$/);
  return match ? match[1].trim() : PRIMARY_MODEL_FALLBACK;
}
