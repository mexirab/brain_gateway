import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { friendlyError } from './errors';

describe('friendlyError', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns the fallback instead of leaking the raw error', () => {
    expect(
      friendlyError(new Error('API 500: Internal Server Error'), 'Could not load.'),
    ).toBe('Could not load.');
  });

  it('logs the raw error for debugging', () => {
    const err = new Error('boom');
    friendlyError(err, 'fallback');
    expect(console.error).toHaveBeenCalledWith(err);
  });

  it('surfaces a 4xx validation detail when preferDetail is set', () => {
    const err = Object.assign(new Error('x'), { status: 400, detail: 'Invalid cron expression' });
    expect(friendlyError(err, 'fallback', { preferDetail: true })).toBe('Invalid cron expression');
  });

  it('hides 5xx detail even with preferDetail', () => {
    const err = Object.assign(new Error('x'), { status: 500, detail: 'stack trace' });
    expect(friendlyError(err, 'fallback', { preferDetail: true })).toBe('fallback');
  });

  it('ignores detail when preferDetail is not set', () => {
    const err = Object.assign(new Error('x'), { status: 400, detail: 'detail' });
    expect(friendlyError(err, 'fallback')).toBe('fallback');
  });
});
