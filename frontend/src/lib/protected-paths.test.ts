import { describe, expect, it } from 'vitest';
import { DEFAULT_LOGIN_REDIRECT, isProtectedPath, safeRedirect } from './protected-paths';

describe('isProtectedPath', () => {
  it('matches a route and its subpaths only', () => {
    expect(isProtectedPath('/chat')).toBe(true);
    expect(isProtectedPath('/finance/transactions')).toBe(true);
    expect(isProtectedPath('/chatty')).toBe(false);
    expect(isProtectedPath('/login')).toBe(false);
    expect(isProtectedPath('/')).toBe(false);
  });
});

describe('safeRedirect', () => {
  it.each([
    ['/personal-facts', '/personal-facts'],
    ['/chat', '/chat'],
    ['/finance/transactions?x=1#top', '/finance/transactions?x=1#top'],
    ['/pantry?tab=scan', '/pantry?tab=scan'],
  ])('keeps the app path %s', (raw, expected) => {
    expect(safeRedirect(raw)).toBe(expected);
  });

  it.each([
    // nothing / not a path
    null,
    undefined,
    '',
    'chat',
    // other origins
    'https://evil.example',
    'https://evil.example/chat',
    '//evil.example',
    '//evil.example/chat',
    '/\\evil.example',
    '/\\evil.example/chat',
    '\\\\evil.example',
    '/\t/evil.example',
    '/\n/evil.example/chat',
    // script / data schemes
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    ' javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    // same origin but not an app page
    '/login',
    '/api/auth/logout',
    '/api/proxy/health',
    '/dashboard/../api/auth/logout',
    '/design-system',
    '/',
  ])('falls back to the dashboard for %j', (raw) => {
    expect(safeRedirect(raw)).toBe(DEFAULT_LOGIN_REDIRECT);
  });
});
