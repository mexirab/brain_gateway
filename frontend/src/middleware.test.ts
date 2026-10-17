// @vitest-environment node
import { readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NextRequest } from 'next/server';
import { expectedSessionValue } from './lib/dashboard-auth';
import { PROTECTED_PATHS, config, middleware } from './middleware';

// Every top-level route under app/(private)/ renders the private shell. Next
// requires `config.matcher` to be a static literal, so it can't be generated
// from the filesystem — this test is the guard that keeps a new page from
// shipping unauthenticated (as /personal-facts once did).
//
// Nested route groups like `(private)/(admin)/audit` contribute `/audit`, so
// recurse into them. `_private` folders, intercepting routes (`(.)foo`) and
// parallel-route slots (`@modal`) aren't standalone URLs. A top-level dynamic
// segment (`[id]`) would surface as `/[id]` and fail on purpose: a prefix list
// can't protect it, so that needs a design change, not a list entry.
function topLevelRoutes(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true })
    .filter((d) => d.isDirectory() && !d.name.startsWith('_') && !d.name.startsWith('@'))
    .flatMap((d) => {
      if (d.name.startsWith('(.')) return [];
      if (d.name.startsWith('(')) return topLevelRoutes(join(dir, d.name));
      return [`/${d.name}`];
    });
}

const privateRoutes = topLevelRoutes(fileURLToPath(new URL('./app/(private)', import.meta.url)));
const validToken = 'test-dashboard-token';
let session = '';
beforeEach(async () => {
  vi.stubEnv('DASHBOARD_TOKEN', validToken);
  session = (await expectedSessionValue())!;
});
afterEach(() => {
  vi.unstubAllEnvs();
});

function request(path: string, token?: string): NextRequest {
  const req = new NextRequest(`http://localhost:3001${path}`);
  if (token !== undefined) req.cookies.set('brain_token', token);
  return req;
}

function expectLoginRedirect(res: Response, path: string) {
  expect(res.status).toBe(307);
  const location = new URL(res.headers.get('location') ?? '');
  expect(location.pathname).toBe('/login');
  expect(location.searchParams.get('redirect')).toBe(path);
}

describe('middleware route protection', () => {
  it('finds the private routes', async () => {
    expect(privateRoutes.length).toBeGreaterThan(0);
  });

  it('keeps config.matcher in sync with PROTECTED_PATHS', async () => {
    // A path in PROTECTED_PATHS but not the matcher never reaches middleware().
    expect([...config.matcher].sort()).toEqual(PROTECTED_PATHS.map((p) => `${p}/:path*`).sort());
  });

  it.each(privateRoutes)('%s is in PROTECTED_PATHS', async (route) => {
    expect(PROTECTED_PATHS).toContain(route);
  });

  it.each(privateRoutes)('%s has a matcher entry', async (route) => {
    expect(config.matcher).toContain(`${route}/:path*`);
  });

  it.each(privateRoutes)('%s redirects an anonymous request to /login', async (route) => {
    expectLoginRedirect(await middleware(request(route)), route);
  });

  it('redirects a wrong token to /login', async () => {
    expectLoginRedirect(await middleware(request('/personal-facts', `${session}x`)), '/personal-facts');
  });

  it('protects nested pages and keeps the full path in redirect', async () => {
    expectLoginRedirect(await middleware(request('/finance/transactions')), '/finance/transactions');
  });

  it('keeps the query string in redirect', async () => {
    expectLoginRedirect(await middleware(request('/pantry?tab=scan')), '/pantry?tab=scan');
  });

  it('lets a request with the valid token through', async () => {
    const res = await middleware(request('/personal-facts', session));
    expect(res.headers.get('x-middleware-next')).toBe('1');
    expect(res.headers.get('location')).toBeNull();
  });

  it.each([undefined, '', 'changeme'])('fails closed when DASHBOARD_TOKEN is %j', async (configured) => {
    vi.stubEnv('DASHBOARD_TOKEN', configured as string);
    for (const cookie of [undefined, '', 'changeme']) {
      expectLoginRedirect(await middleware(request('/chat', cookie)), '/chat');
    }
  });

  it('leaves public routes alone', async () => {
    for (const path of ['/', '/login', '/design-system']) {
      expect((await middleware(request(path))).headers.get('x-middleware-next')).toBe('1');
    }
  });
});
