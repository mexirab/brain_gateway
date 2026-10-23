import { describe, it, expect, beforeEach } from 'vitest';
import { DEFAULT_THEME, THEME_COOKIE, parseThemePref, setThemePref } from './theme';

describe('parseThemePref', () => {
  it.each(['dark', 'light', 'system'] as const)('accepts %s', (v) => {
    expect(parseThemePref(v)).toBe(v);
  });

  it.each([undefined, null, '', 'Dark', 'blue', 'dark; Path=/', ' light'])(
    'falls back to dark for %j',
    (v) => {
      expect(parseThemePref(v)).toBe('dark');
    },
  );

  it('default is dark', () => {
    expect(DEFAULT_THEME).toBe('dark');
  });
});

describe('setThemePref', () => {
  beforeEach(() => {
    document.cookie = `${THEME_COOKIE}=; Path=/; Max-Age=0`;
    delete document.documentElement.dataset.theme;
  });

  it('writes the cookie with path, one-year max-age and SameSite=Lax', () => {
    const writes: string[] = [];
    const desc = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie')!;
    Object.defineProperty(document, 'cookie', {
      configurable: true,
      get: () => desc.get!.call(document),
      set: (v: string) => {
        writes.push(v);
        desc.set!.call(document, v);
      },
    });
    try {
      setThemePref('light');
    } finally {
      delete (document as unknown as Record<string, unknown>).cookie;
    }
    expect(writes).toEqual([`jess-theme=light; Path=/; Max-Age=${60 * 60 * 24 * 365}; SameSite=Lax`]);
    expect(document.cookie).toContain('jess-theme=light');
  });

  it('sets data-theme on <html>', () => {
    setThemePref('system');
    expect(document.documentElement.dataset.theme).toBe('system');
    setThemePref('dark');
    expect(document.documentElement.dataset.theme).toBe('dark');
  });
});
