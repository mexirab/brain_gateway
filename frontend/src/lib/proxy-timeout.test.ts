import { describe, expect, it } from 'vitest';
import {
  CHAT_UPSTREAM_TIMEOUT_MS,
  DEFAULT_UPSTREAM_TIMEOUT_MS,
  SLOW_UPSTREAM_TIMEOUT_MS,
  upstreamTimeoutMs,
} from './proxy-timeout';

describe('upstreamTimeoutMs', () => {
  it('gives chat completions the long budget', () => {
    expect(upstreamTimeoutMs(['v1', 'chat', 'completions'])).toBe(CHAT_UPSTREAM_TIMEOUT_MS);
  });

  it.each([
    [['api', 'meals', 'photo']],
    [['api', 'finance', 'sync']],
    [['api', 'finance', 'categories']],
    [['api', 'self_audit', 'run']],
    [['api', 'paperless', 'upload']],
    [['api', 'documents']],
    [['api', 'documents', 'suggest-name']],
  ])('%j gets the slow budget', (path) => {
    expect(upstreamTimeoutMs(path)).toBe(SLOW_UPSTREAM_TIMEOUT_MS);
  });

  it.each([
    [['api', 'tasks']],
    [['v1', 'models']],
    [['api', 'v1', 'chat', 'completions']],
    [['api', 'meals', 'photo', 'x']],
    [['api', 'finance', 'sync', 'status']],
    [[]],
  ])('%j gets the default budget', (path) => {
    expect(upstreamTimeoutMs(path)).toBe(DEFAULT_UPSTREAM_TIMEOUT_MS);
  });

  it('keeps every slow budget above the orchestrator inner 120 s timeouts', () => {
    expect(SLOW_UPSTREAM_TIMEOUT_MS).toBeGreaterThan(120_000);
  });
});
