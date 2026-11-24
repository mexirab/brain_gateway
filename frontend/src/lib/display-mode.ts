// Display-only mode for unattended screens: the page with no sidebar and no bottom tab bar. The
// closet kiosk on Jupiter (scripts/jupiter-kiosk) turns it on by setting this cookie in its own
// browser profile. There is no switch for it in the UI. Hiding the menus is cosmetic: every route
// still needs a session. What limits the kiosk is its own read-only session (lib/kiosk-session.ts),
// which also hides the menus on its own.
//
// The __Host- prefix makes the browser refuse the cookie unless it is Secure, Path=/ and host-only,
// so another host on the tailnet can't plant it (Domain=tail74fc4a.ts.net) and strip a phone's menus.
export const DISPLAY_COOKIE = '__Host-jess_display';

/** True only for the exact value the kiosk sets; anything else keeps the normal layout. */
export function isKioskDisplay(value: string | null | undefined): boolean {
  return value === 'kiosk';
}
