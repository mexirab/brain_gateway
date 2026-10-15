// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NextRequest } from 'next/server';
import { GET, POST } from './route';

const TOKEN = 'test-dashboard-token';
const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubEnv('DASHBOARD_TOKEN', TOKEN);
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(Response.json({ ok: true }));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

function req(url: string, cookie: string | null = TOKEN, init?: { method: string; body: string }) {
  const r = new NextRequest(`http://localhost:3001${url}`, init);
  if (cookie !== null) r.cookies.set('brain_token', cookie);
  return r;
}

const ctx = (path: string[]) => ({ params: Promise.resolve({ path }) });

// The route attaches the API_TOKEN bearer to whatever it fetches, so the
// property that matters is: a refused path never reaches fetch at all.
describe('/api/proxy route', () => {
  it.each([
    [['/evil.example', 'x']],
    [['\\evil.example', 'x']],
    [['api', '..', 'metrics']],
    [['metrics']],
  ])('GET %j is refused without fetching', async (path) => {
    const res = await GET(req('/api/proxy/x'), ctx(path));
    expect(res.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('POST to a refused path is refused without fetching', async () => {
    const res = await POST(req('/api/proxy/x', TOKEN, { method: 'POST', body: '{}' }), ctx(['/evil.example', 'x']));
    expect(res.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([null, '', 'changeme', 'wrong'])('cookie %j gets 401 without fetching', async (cookie) => {
    const res = await GET(req('/api/proxy/health', cookie), ctx(['health']));
    expect(res.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('forwards an allowed GET, with its query, to the orchestrator origin', async () => {
    const res = await GET(req('/api/proxy/api/tasks?status=open'), ctx(['api', 'tasks']));
    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url] = fetchMock.mock.calls[0];
    expect(new URL(url).pathname + new URL(url).search).toBe('/api/tasks?status=open');
  });
});
