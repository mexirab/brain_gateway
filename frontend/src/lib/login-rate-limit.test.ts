import { beforeEach, describe, expect, it } from 'vitest';
import {
  MAX_FAILURES_PER_CLIENT,
  beginAttempt,
  MAX_FAILURES_TOTAL,
  _resetLoginRateLimit,
  clientKey,
  recordFailure,
  recordSuccess,
  retryAfterSeconds,
} from './login-rate-limit';

beforeEach(() => _resetLoginRateLimit());

describe('login rate limit', () => {
  it('locks a client out after too many failures, until the window passes', () => {
    const t0 = 1_000_000;
    for (let i = 0; i < MAX_FAILURES_PER_CLIENT; i++) recordFailure('a', t0);
    expect(retryAfterSeconds('a', t0)).toBeGreaterThan(0);
    expect(retryAfterSeconds('b', t0)).toBe(0); // other clients unaffected
    expect(retryAfterSeconds('a', t0 + 15 * 60 * 1000)).toBe(0);
  });

  it('a successful login clears the client', () => {
    for (let i = 0; i < MAX_FAILURES_PER_CLIENT - 1; i++) recordFailure('a');
    recordSuccess('a');
    recordFailure('a');
    expect(retryAfterSeconds('a')).toBe(0);
  });

  it('caps total failures even when the client key keeps changing', () => {
    const t0 = 2_000_000;
    for (let i = 0; i < MAX_FAILURES_TOTAL; i++) recordFailure(`spoofed-${i}`, t0);
    expect(retryAfterSeconds('fresh-client', t0)).toBeGreaterThan(0);
  });

  it('client key comes from the last forwarded hop', () => {
    // Serve appends the real peer; earlier hops are whatever the client claimed
    expect(clientKey(new Headers({ 'x-forwarded-for': '1.2.3.4, 100.64.0.5' }))).toBe('100.64.0.5');
    expect(clientKey(new Headers())).toBe('direct');
  });
});

describe('beginAttempt', () => {
  it('counts the attempt up front and lets a success give it back', () => {
    for (let i = 0; i < MAX_FAILURES_PER_CLIENT; i++) expect(beginAttempt('k')).toBe(0);
    expect(beginAttempt('k')).toBeGreaterThan(0);
    _resetLoginRateLimit();
    for (let i = 0; i < MAX_FAILURES_TOTAL + 5; i++) {
      expect(beginAttempt('owner')).toBe(0);
      recordSuccess('owner'); // successful logins never use up the global cap
    }
  });
});
