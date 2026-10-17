// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NextRequest } from 'next/server';
import { expectedSessionValue } from '@/lib/dashboard-auth';
import { MAX_FAILURES_PER_CLIENT, _resetLoginRateLimit } from '@/lib/login-rate-limit';
import { POST } from './route';

const TOKEN = 'login-test-password';

beforeEach(() => {
  vi.stubEnv('DASHBOARD_TOKEN', TOKEN);
  _resetLoginRateLimit();
});
afterEach(() => vi.unstubAllEnvs());

function login(password: unknown, headers: Record<string, string> = {}, url = 'http://localhost:3001') {
  return POST(
    new NextRequest(`${url}/api/auth/login`, {
      method: 'POST',
      body: JSON.stringify({ password }),
      headers: { 'content-type': 'application/json', 'sec-fetch-site': 'same-origin', ...headers },
    }),
  );
}

describe('POST /api/auth/login', () => {
  it('sets the derived session cookie, not the password', async () => {
    const res = await login(TOKEN);
    expect(res.status).toBe(200);
    const cookie = res.cookies.get('brain_token');
    expect(cookie?.value).toBe(await expectedSessionValue());
    expect(cookie?.value).not.toBe(TOKEN);
    expect(cookie?.httpOnly).toBe(true);
    expect(cookie?.secure).toBeFalsy(); // plain http
  });

  it('marks the cookie Secure behind HTTPS (Tailscale Serve)', async () => {
    const res = await login(TOKEN, { 'x-forwarded-proto': 'https' });
    expect(res.cookies.get('brain_token')?.secure).toBe(true);
  });

  it('rejects a wrong password without a cookie', async () => {
    const res = await login('nope');
    expect(res.status).toBe(401);
    expect(res.cookies.get('brain_token')).toBeUndefined();
  });

  it('locks a client out after repeated failures, even for the right password', async () => {
    const ip = { 'x-forwarded-for': '100.64.1.2' };
    for (let i = 0; i < MAX_FAILURES_PER_CLIENT; i++) expect((await login('nope', ip)).status).toBe(401);
    const res = await login(TOKEN, ip);
    expect(res.status).toBe(429);
    expect(Number(res.headers.get('retry-after'))).toBeGreaterThan(0);
    expect((await login(TOKEN, { 'x-forwarded-for': '100.64.9.9' })).status).toBe(200);
  });

  it('refuses a cross-site login', async () => {
    const res = await login(TOKEN, { 'sec-fetch-site': 'cross-site' });
    expect(res.status).toBe(403);
  });

  it('fails closed when no password is configured', async () => {
    vi.stubEnv('DASHBOARD_TOKEN', 'changeme');
    expect((await login('changeme')).status).toBe(503);
  });
});

describe('burst protection', () => {
  it('a burst of parallel wrong guesses only gets 5 password checks', async () => {
    const ip = { 'x-forwarded-for': '100.64.3.3' };
    const results = await Promise.all(Array.from({ length: 20 }, () => login('nope', ip)));
    const statuses = results.map((r) => r.status);
    expect(statuses.filter((s) => s === 401)).toHaveLength(MAX_FAILURES_PER_CLIENT);
    expect(statuses.filter((s) => s === 429)).toHaveLength(20 - MAX_FAILURES_PER_CLIENT);
  });
});
