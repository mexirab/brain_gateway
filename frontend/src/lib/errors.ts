/**
 * Convert any thrown value into user-safe copy.
 *
 * The API layer throws technical strings like `API 500: Internal Server Error`
 * and `fetch failed`. Rendering those straight to the UI makes a consumer
 * product read as broken. This logs the real error (for debugging) and returns
 * a friendly fallback to show the user instead.
 *
 * Always pass a context-specific fallback ("Couldn't save your meal.") so the
 * message tells the user what failed without exposing internals.
 *
 * `preferDetail` opts into surfacing a recoverable validation message: if the
 * error carries an HTTP `status` in the 4xx range and a non-empty `detail`
 * (see `SettingsApiError`), that detail is returned instead of the fallback.
 * Use it on settings/form mutations where the backend explains what to fix
 * ("Unknown selfcare categories…", an invalid cron). 5xx/network still fall
 * back to the friendly copy.
 */
export function friendlyError(
  err: unknown,
  fallback = "Something didn't work. Give it a moment and try again.",
  opts?: { preferDetail?: boolean },
): string {
  if (err) console.error(err);
  if (opts?.preferDetail && err && typeof err === 'object') {
    const status = (err as { status?: unknown }).status;
    const detail = (err as { detail?: unknown }).detail;
    if (typeof status === 'number' && status >= 400 && status < 500 && typeof detail === 'string' && detail) {
      return detail;
    }
  }
  return fallback;
}
