// Theme preference — a per-device choice stored in a plain cookie so the
// server renders <html data-theme> correctly on the first byte (no flash of
// the wrong palette). Palettes themselves: src/styles/tokens.css.

export const THEME_COOKIE = 'jess-theme';

export const THEME_PREFS = ['dark', 'light', 'system'] as const;
export type ThemePref = (typeof THEME_PREFS)[number];

// Dark stays the default so existing installs look the same until someone
// picks otherwise.
export const DEFAULT_THEME: ThemePref = 'dark';

export function parseThemePref(value: string | undefined | null): ThemePref {
  return (THEME_PREFS as readonly string[]).includes(value ?? '')
    ? (value as ThemePref)
    : DEFAULT_THEME;
}

/** Apply a preference on this device: cookie for the server, attribute for now. */
export function setThemePref(pref: ThemePref): void {
  const oneYear = 60 * 60 * 24 * 365;
  document.cookie = `${THEME_COOKIE}=${pref}; Path=/; Max-Age=${oneYear}; SameSite=Lax`;
  document.documentElement.dataset.theme = pref;
}
