import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  _resetKioskTokenWarning,
  dashboardSessionKind,
  expectedDashboardToken,
  expectedKioskSessionValue,
  expectedKioskToken,
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
    expect(await isCorrectPassword('')).toBe(false); // resolves (no throw on a zero-length HMAC key)
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

describe('kiosk session', () => {
  const DASH = 's3cret-dashboard-token';
  const KIOSK = 'kiosk-token-0123456789abcdef';

  // Rest parameter, not a default: a test passes `undefined` on purpose to unset DASHBOARD_TOKEN.
  function configure(kiosk: string | undefined, ...dashboard: [string | undefined] | []) {
    vi.stubEnv('DASHBOARD_TOKEN', dashboard.length ? dashboard[0] : DASH);
    vi.stubEnv('KIOSK_TOKEN', kiosk);
  }

  it('is its own kind, derived from KIOSK_TOKEN, never a full session', async () => {
    configure(KIOSK);
    const full = (await expectedSessionValue())!;
    const kiosk = (await expectedKioskSessionValue())!;
    expect(kiosk).toMatch(/^[0-9a-f]{64}$/);
    expect(kiosk).not.toBe(full);
    expect(kiosk).not.toContain(KIOSK);
    expect(await dashboardSessionKind(full)).toBe('full');
    expect(await dashboardSessionKind(kiosk)).toBe('kiosk');
    expect(await isValidDashboardSession(kiosk)).toBe(false); // the full-only check never admits a kiosk
    expect(await isValidDashboardSession(full)).toBe(true);
    expect(await dashboardSessionKind(KIOSK)).toBeNull(); // the raw token is not a session
    expect(await dashboardSessionKind(`${kiosk} `)).toBeNull();
    expect(await dashboardSessionKind(undefined)).toBeNull();
  });

  it('KIOSK_TOKEN is not a password', async () => {
    configure(KIOSK);
    expect(await isCorrectPassword(KIOSK)).toBe(false);
    expect(await isCorrectPassword(DASH)).toBe(true);
  });

  it('changing KIOSK_TOKEN logs out only the kiosk', async () => {
    configure(KIOSK);
    const full = await expectedSessionValue();
    const oldKiosk = await expectedKioskSessionValue();
    configure('another-kiosk-token-0123456789');
    expect(await dashboardSessionKind(oldKiosk)).toBeNull();
    expect(await dashboardSessionKind(full)).toBe('full');
  });

  it.each([
    ['unset', undefined],
    ['empty', ''],
    ['blank', '    '],
    ['changeme', 'changeme'],
    ['23 characters', 'x'.repeat(23)],
    ['the dashboard token', DASH.padEnd(24, '!')],
  ])('is off when KIOSK_TOKEN is %s', async (_label, value) => {
    const dashboard = value?.startsWith(DASH) ? value : DASH;
    configure(value, dashboard);
    expect(expectedKioskToken()).toBeNull();
    expect(await expectedKioskSessionValue()).toBeNull();
    for (const candidate of [undefined, '', 'changeme', value]) {
      expect(await dashboardSessionKind(candidate)).not.toBe('kiosk');
    }
  });

  it('accepts a 24-character token, trimmed like DASHBOARD_TOKEN', async () => {
    configure(`  ${'k'.repeat(24)}\n`);
    expect(expectedKioskToken()).toBe('k'.repeat(24));
  });

  it.each([undefined, '', 'changeme'])('is off when the dashboard has no login (DASHBOARD_TOKEN %j)', async (dash) => {
    configure(KIOSK, dash);
    expect(expectedKioskToken()).toBeNull();
    expect(await expectedKioskSessionValue()).toBeNull();
  });
});

describe('an ignored KIOSK_TOKEN', () => {
  it.each([
    ['too_short', 'x'.repeat(23), 'dashboard-token'],
    ['same_as_dashboard', 'same-token-0123456789abcdef', 'same-token-0123456789abcdef'],
    ['no_dashboard_token', 'kiosk-token-0123456789abcdef', ''],
  ])('warns once (%s), with the reason and never the value', (reason, kiosk, dash) => {
    _resetKioskTokenWarning();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.stubEnv('KIOSK_TOKEN', kiosk);
    vi.stubEnv('DASHBOARD_TOKEN', dash);
    expect(expectedKioskToken()).toBeNull();
    expect(expectedKioskToken()).toBeNull();
    expect(warn).toHaveBeenCalledTimes(1);
    const line = JSON.parse(String(warn.mock.calls[0][0]));
    expect(line).toMatchObject({ level: 'warning', logger: 'frontend', reason });
    expect(String(warn.mock.calls[0][0])).not.toContain(kiosk);
    warn.mockRestore();
  });

  it('says nothing when KIOSK_TOKEN is simply not set', () => {
    _resetKioskTokenWarning();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.stubEnv('KIOSK_TOKEN', '');
    vi.stubEnv('DASHBOARD_TOKEN', 'dashboard-token');
    expect(expectedKioskToken()).toBeNull();
    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });
});
