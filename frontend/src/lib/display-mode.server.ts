import { cookies } from 'next/headers';
import { DISPLAY_COOKIE, isKioskDisplay } from './display-mode';

/** Server components: is this request from a display-only screen (the closet kiosk)? */
export function isKioskDisplayRequest(): boolean {
  return isKioskDisplay(cookies().get(DISPLAY_COOKIE)?.value);
}
