import { describe, expect, it } from 'vitest';
import { DISPLAY_COOKIE, isKioskDisplay } from './display-mode';

describe('display mode', () => {
  it('uses the cookie name the kiosk sets (scripts/jupiter-kiosk/kiosk.py DISPLAY_COOKIE)', () => {
    expect(DISPLAY_COOKIE).toBe('__Host-jess_display');
  });

  it('turns on only for the exact value "kiosk"', () => {
    expect(isKioskDisplay('kiosk')).toBe(true);
  });

  it.each([undefined, null, '', 'Kiosk', 'KIOSK', ' kiosk', 'kiosk ', '1', 'true', 'normal'])(
    'keeps the normal layout for %j',
    (value) => {
      expect(isKioskDisplay(value)).toBe(false);
    },
  );
});
