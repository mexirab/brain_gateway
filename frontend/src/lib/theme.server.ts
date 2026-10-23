import { cookies } from 'next/headers';
import { THEME_COOKIE, parseThemePref, type ThemePref } from './theme';

export function getThemePref(): ThemePref {
  return parseThemePref(cookies().get(THEME_COOKIE)?.value);
}
