import { afterEach, describe, expect, it, vi } from 'vitest';
import { expectedDashboardToken, isValidDashboardToken } from './dashboard-auth';

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('dashboard token', () => {
  it('accepts the configured token only', () => {
    vi.stubEnv('DASHBOARD_TOKEN', 's3cret-token');
    expect(expectedDashboardToken()).toBe('s3cret-token');
    expect(isValidDashboardToken('s3cret-token')).toBe(true);
    expect(isValidDashboardToken('s3cret-token ')).toBe(false);
    expect(isValidDashboardToken('changeme')).toBe(false);
    expect(isValidDashboardToken(undefined)).toBe(false);
  });

  it.each(['', '   ', 'changeme', ' changeme '])('fails closed when DASHBOARD_TOKEN is %j', (value) => {
    vi.stubEnv('DASHBOARD_TOKEN', value);
    expect(expectedDashboardToken()).toBeNull();
    for (const candidate of [undefined, null, '', 'changeme', value]) {
      expect(isValidDashboardToken(candidate)).toBe(false);
    }
  });

  it('fails closed when DASHBOARD_TOKEN is missing', () => {
    vi.stubEnv('DASHBOARD_TOKEN', undefined);
    expect(expectedDashboardToken()).toBeNull();
    expect(isValidDashboardToken('changeme')).toBe(false);
  });
});
