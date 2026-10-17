// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { NextRequest } from 'next/server';
import { POST } from './route';

const logout = (headers: Record<string, string>) =>
  POST(new NextRequest('http://localhost:3001/api/auth/logout', { method: 'POST', headers }));

describe('POST /api/auth/logout', () => {
  it('sends a form submission back to /login and clears the cookie', async () => {
    const res = await logout({ 'sec-fetch-site': 'same-origin', accept: 'text/html,*/*' });
    expect(res.status).toBe(303);
    expect(res.headers.get('location')).toBe('/login');
    expect(res.cookies.get('brain_token')?.value).toBe('');
  });

  it('answers JSON to fetch callers', async () => {
    const res = await logout({ 'sec-fetch-site': 'same-origin', accept: 'application/json' });
    expect(res.status).toBe(200);
  });

  it('refuses a cross-site logout', async () => {
    expect((await logout({ 'sec-fetch-site': 'cross-site' })).status).toBe(403);
  });
});
