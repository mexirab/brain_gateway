import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  expectedDashboardToken,
  expectedSessionValue,
  isCorrectPassword,
  isValidDashboardSession,
  timingSafeEqual,
} from './dashboard-auth';

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('dashboard token + session', () => {
  it('the cookie is derived from the password, never the password itself', async () => {
    vi.stubEnv('DASHBOARD_TOKEN', 's3cret-token');
    const session = await expectedSessionValue();
    expect(session).toMatch(/^[0-9a-f]{64}$/);
    expect(session).not.toContain('s3cret-token');
    expect(await isValidDashboardSession(session)).toBe(true);
    expect(await isValidDashboardSession('s3cret-token')).toBe(false); // the raw password is not a session
    expect(await isValidDashboardSession(`${session} `)).toBe(false);
    expect(await isValidDashboardSession(undefined)).toBe(false);
  });

  it('changing the password invalidates old sessions', async () => {
    vi.stubEnv('DASHBOARD_TOKEN', 'first-password');
    const old = await expectedSessionValue();
    vi.stubEnv('DASHBOARD_TOKEN', 'second-password');
    expect(await isValidDashboardSession(old)).toBe(false);
  });

  it('checks the password exactly', async () => {
    vi.stubEnv('DASHBOARD_TOKEN', 's3cret-token');
    expect(await isCorrectPassword('s3cret-token')).toBe(true);
    expect(await isCorrectPassword('s3cret-token ')).toBe(false);
    expect(await isCorrectPassword('S3cret-token')).toBe(false);
    expect(await isCorrectPassword(42)).toBe(false);
    expect(await isCorrectPassword(undefined)).toBe(false);
  });

  it.each(['', '   ', 'changeme', ' changeme '])('fails closed when DASHBOARD_TOKEN is %j', async (value) => {
    vi.stubEnv('DASHBOARD_TOKEN', value);
    expect(expectedDashboardToken()).toBeNull();
    expect(await expectedSessionValue()).toBeNull();
    for (const candidate of [undefined, null, '', 'changeme', value]) {
      expect(await isValidDashboardSession(candidate)).toBe(false);
      expect(await isCorrectPassword(candidate)).toBe(false);
    }
  });

  it('fails closed when DASHBOARD_TOKEN is missing', async () => {
    vi.stubEnv('DASHBOARD_TOKEN', undefined);
    expect(expectedDashboardToken()).toBeNull();
    expect(await isCorrectPassword('changeme')).toBe(false);
  });

  it('timingSafeEqual compares fully', () => {
    expect(timingSafeEqual('abc', 'abc')).toBe(true);
    expect(timingSafeEqual('abc', 'abd')).toBe(false);
    expect(timingSafeEqual('abc', 'abcd')).toBe(false);
    expect(timingSafeEqual('', '')).toBe(true);
  });
});
