import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import DashboardPage from '@/app/(private)/dashboard/page';
import { KIOSK_PAGES, KIOSK_PROXY_GETS, kioskMayOpenPage, kioskMayProxy } from './kiosk-session';

const seen: { url: string; method: string }[] = [];

beforeEach(() => {
  seen.length = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      seen.push({ url: String(input), method: (init?.method ?? 'GET').toUpperCase() });
      // Every card goes to its error state; the hooks still issue all their requests (Promise.all).
      // Polls re-run the same SWR fetchers (and SWR doesn't poll a key in error), and no card has a
      // timer or fetch of its own, so the load is the whole list. Blind spot: a request a card would
      // only make once real data has arrived isn't triggered here.
      return new Response(JSON.stringify({ error: 'test' }), { status: 503 });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// The kiosk's allow-list must be the dashboard page's real call list. When a card is added to
// app/(private)/dashboard/page.tsx (or a card's URL changes), this fails: decide whether a
// read-only screen may show it, then update KIOSK_PROXY_GETS — or the kiosk's card shows a 403.
describe('KIOSK_PROXY_GETS matches the dashboard page', () => {
  const expected = [...KIOSK_PROXY_GETS].map((p) => `/api/proxy${p}`).sort();
  const urls = () => [...new Set(seen.map((s) => s.url))].sort();

  it('is exactly the GETs the page makes', async () => {
    render(
      <SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false, dedupingInterval: 0 }}>
        <DashboardPage />
      </SWRConfig>,
    );
    await waitFor(() => expect(urls()).toEqual(expected));
    expect(seen.every((s) => s.method === 'GET')).toBe(true);
  });
});

describe('kioskMayOpenPage', () => {
  it('allows only /dashboard itself', () => {
    expect(KIOSK_PAGES).toEqual(['/dashboard']);
    expect(kioskMayOpenPage('/dashboard')).toBe(true);
    for (const path of ['/dashboard/', '/dashboard/x', '/dashboardx', '/chat', '/settings', '/home', '/avatar/jess.vrm', '/']) {
      expect(kioskMayOpenPage(path)).toBe(false);
    }
  });
});

describe('kioskMayProxy', () => {
  it.each(KIOSK_PROXY_GETS.map((p) => [p]))('GET %s is allowed', (entry) => {
    const [path, query] = entry.split('?');
    expect(kioskMayProxy('GET', path.slice(1).split('/'), query ? `?${query}` : '')).toBe(true);
  });

  // At the route, Next hands HEAD to the GET export, so a HEAD arrives here as 'GET' (the same read).
  it.each(['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'get'])('%s is refused even on an allowed path', (m) => {
    expect(kioskMayProxy(m, ['api', 'reminders'], '')).toBe(false);
  });

  it.each([
    [['api', 'inbox'], ''],
    [['v1', 'chat', 'completions'], ''],
    [['api', 'ha', 'command'], ''],
    [['api', 'ha', 'entities'], ''],
    [['api', 'config', 'personal-facts'], ''],
    [['api', 'tasks'], ''],
    [['api', 'tasks'], '?status=done'],
    [['api', 'tasks'], '?status=open&x=1'],
    [['api', 'announcements', 'history'], '?limit=200'],
    [['api', 'announcements', 'history'], ''],
    [['api', 'reminders'], '?x=1'],
    [['api', 'reminders', ''], ''],
    [['api/reminders'], ''],
    [['api', 'tasks?status=open'], ''], // a decoded %3F: joined it would match, upstream it's a different path
    [['api', 'reminders#x'], ''],
    [['api', 'reminders%2F'], ''],
    [['api', 'progress\\today'], ''],
    [['health', 'x'], ''],
    [[], ''],
  ])('GET %j %j is refused', (segments, search) => {
    expect(kioskMayProxy('GET', segments, search)).toBe(false);
  });
});
