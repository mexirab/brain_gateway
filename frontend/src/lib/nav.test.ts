import { describe, it, expect } from 'vitest';
import { NAV_ITEMS, isNavActive } from './nav';
import { PROTECTED_PATHS } from './protected-paths';

describe('isNavActive', () => {
  it('matches the exact path', () => {
    expect(isNavActive('/finance', '/finance')).toBe(true);
  });

  it('matches nested paths', () => {
    expect(isNavActive('/finance/boss-battle', '/finance')).toBe(true);
  });

  it('does not match a sibling that merely shares a prefix', () => {
    expect(isNavActive('/financeX', '/finance')).toBe(false);
    expect(isNavActive('/homepage', '/home')).toBe(false);
  });

  it('is false for a null or empty pathname', () => {
    expect(isNavActive(null, '/finance')).toBe(false);
    expect(isNavActive('', '/finance')).toBe(false);
  });

  it('does not match a different page', () => {
    expect(isNavActive('/chat', '/finance')).toBe(false);
  });
});

describe('NAV_ITEMS', () => {
  it('has unique hrefs', () => {
    const hrefs = NAV_ITEMS.map((i) => i.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });

  it('has exactly the five mobile primary tabs, in order', () => {
    expect(NAV_ITEMS.filter((i) => i.primary).map((i) => i.label)).toEqual([
      'Dashboard',
      'Chat',
      'Tasks',
      'Meals',
      'Workouts',
    ]);
  });

  it('every href is a protected (login-gated) path', () => {
    for (const { href } of NAV_ITEMS) {
      expect(PROTECTED_PATHS, href).toContain(href);
    }
  });

  it('every item has a label and an icon', () => {
    for (const item of NAV_ITEMS) {
      expect(item.label.trim()).not.toBe('');
      expect(item.icon).toBeTruthy();
    }
  });
});
