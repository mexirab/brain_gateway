import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { DEFAULT_ASSISTANT_NAME, getAssistantName } from './branding.server';

function jsonResponse(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body } as unknown as Response;
}

describe('getAssistantName', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'warn').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('returns the trimmed assistant name', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ assistant_name: '  Nova  ' }));
    await expect(getAssistantName()).resolves.toBe('Nova');
  });

  it('caps the name at 64 characters', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ assistant_name: 'x'.repeat(200) }));
    const name = await getAssistantName();
    expect(name).toHaveLength(64);
  });

  it('calls the identity endpoint with a bearer header and an AbortSignal', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ assistant_name: 'Nova' }));
    await getAssistantName();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/api\/config\/identity$/);
    expect(init.headers.Authorization).toMatch(/^Bearer /);
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it('falls back on a non-ok response', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ assistant_name: 'Nova' }, false, 500));
    await expect(getAssistantName()).resolves.toBe(DEFAULT_ASSISTANT_NAME);
  });

  it.each([
    [{ assistant_name: 42 }],
    [{ assistant_name: null }],
    [{}],
    [{ assistant_name: '   ' }],
    [{ assistant_name: '' }],
  ])('falls back on a non-string or blank name: %j', async (body) => {
    fetchMock.mockResolvedValue(jsonResponse(body));
    await expect(getAssistantName()).resolves.toBe('Jess');
  });

  it('falls back when fetch throws', async () => {
    fetchMock.mockRejectedValue(new TypeError('fetch failed'));
    await expect(getAssistantName()).resolves.toBe('Jess');
  });

  it('falls back when the request is aborted (timeout)', async () => {
    fetchMock.mockRejectedValue(new DOMException('The operation timed out.', 'TimeoutError'));
    await expect(getAssistantName()).resolves.toBe('Jess');
  });

  it('falls back when the body is not JSON', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => {
        throw new SyntaxError('bad json');
      },
    } as unknown as Response);
    await expect(getAssistantName()).resolves.toBe('Jess');
  });
});
