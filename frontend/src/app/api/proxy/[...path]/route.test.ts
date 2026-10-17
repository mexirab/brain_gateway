// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NextRequest } from 'next/server';
import { expectedSessionValue } from '@/lib/dashboard-auth';
import { DELETE, GET, POST } from './route';

const TOKEN = 'test-dashboard-token';
const fetchMock = vi.fn();
let SESSION = '';

beforeEach(async () => {
  vi.stubEnv('DASHBOARD_TOKEN', TOKEN);
  SESSION = (await expectedSessionValue())!;
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(Response.json({ ok: true }));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

type Init = { method: string; body?: string; headers?: Record<string, string> };

function req(url: string, cookie: string | null | undefined = undefined, init?: Init) {
  const r = new NextRequest(`http://localhost:3001${url}`, init);
  const value = cookie === undefined ? SESSION : cookie;
  if (value !== null) r.cookies.set('brain_token', value);
  return r;
}

const ctx = (path: string[]) => ({ params: Promise.resolve({ path }) });
const sameOrigin = { 'sec-fetch-site': 'same-origin' };

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
    const res = await POST(
      req('/api/proxy/x', undefined, { method: 'POST', body: '{}', headers: sameOrigin }),
      ctx(['/evil.example', 'x']),
    );
    expect(res.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([null, '', 'changeme', 'wrong', TOKEN])('cookie %j gets 401 without fetching', async (cookie) => {
    // TOKEN: the raw password is no longer a valid session cookie
    const res = await GET(req('/api/proxy/health', cookie), ctx(['health']));
    expect(res.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('forwards an allowed GET, with its query, to the orchestrator origin', async () => {
    const res = await GET(req('/api/proxy/api/tasks?status=open'), ctx(['api', 'tasks']));
    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchMock.mock.calls[0];
    expect(new URL(url).pathname + new URL(url).search).toBe('/api/tasks?status=open');
    expect(opts.redirect).toBe('manual');
  });
});

describe('CSRF on writes', () => {
  it.each(['cross-site', 'same-site'])('POST with Sec-Fetch-Site %s is refused without fetching', async (site) => {
    const res = await POST(
      req('/api/proxy/api/tasks', undefined, { method: 'POST', body: '{}', headers: { 'sec-fetch-site': site } }),
      ctx(['api', 'tasks']),
    );
    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('DELETE from another origin is refused', async () => {
    const res = await DELETE(
      req('/api/proxy/api/tasks/1', undefined, { method: 'DELETE', headers: { origin: 'http://localhost:3000' } }),
      ctx(['api', 'tasks', '1']),
    );
    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('same-origin POST goes through', async () => {
    const res = await POST(
      req('/api/proxy/api/tasks', undefined, { method: 'POST', body: '{"a":1}', headers: sameOrigin }),
      ctx(['api', 'tasks']),
    );
    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('responses', () => {
  const download = (body: string, headers: Record<string, string>, status = 200) =>
    fetchMock.mockResolvedValue(new Response(body, { status, headers }));

  it('streams a PDF through as a download', async () => {
    download('%PDF-1.7', { 'content-type': 'application/pdf', 'content-disposition': 'inline; filename="a.pdf"' });
    const res = await GET(req('/api/proxy/api/documents/1/download'), ctx(['api', 'documents', '1', 'download']));
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('application/pdf');
    expect(res.headers.get('content-disposition')).toBe('attachment; filename="a.pdf"');
    expect(res.headers.get('x-content-type-options')).toBe('nosniff');
    expect(await res.text()).toBe('%PDF-1.7');
  });

  it('never renders HTML from the orchestrator inline', async () => {
    download('<script>alert(1)</script>', { 'content-type': 'text/html' });
    const res = await GET(req('/api/proxy/api/documents/2/download'), ctx(['api', 'documents', '2', 'download']));
    expect(res.headers.get('content-disposition')).toBe('attachment');
    expect(res.headers.get('content-security-policy')).toContain('sandbox');
  });

  it('keeps images inline', async () => {
    download('png', { 'content-type': 'image/png' });
    const res = await GET(req('/api/proxy/api/x.png'), ctx(['api', 'x.png']));
    expect(res.headers.get('content-disposition')).toBeNull();
    expect(res.headers.get('content-security-policy')).toBeNull();
  });

  it('turns a plain-text error page into a JSON 502', async () => {
    download('Internal Server Error', { 'content-type': 'text/plain' }, 500);
    const res = await GET(req('/api/proxy/api/tasks'), ctx(['api', 'tasks']));
    expect(res.status).toBe(502);
    expect((await res.json()).error).toContain('HTTP 500');
  });

  it('turns unparseable JSON into a 502', async () => {
    download('not json', { 'content-type': 'application/json' });
    const res = await GET(req('/api/proxy/api/tasks'), ctx(['api', 'tasks']));
    expect(res.status).toBe(502);
  });

  it('refuses to relay a redirect', async () => {
    download('', { location: 'https://evil.example/' }, 302);
    const res = await GET(req('/api/proxy/api/tasks'), ctx(['api', 'tasks']));
    expect(res.status).toBe(502);
  });

  it('reports an unreachable orchestrator as 502', async () => {
    fetchMock.mockRejectedValue(new TypeError('fetch failed'));
    const res = await GET(req('/api/proxy/api/tasks'), ctx(['api', 'tasks']));
    expect(res.status).toBe(502);
    expect((await res.json()).error).toBe('Orchestrator unreachable');
  });
});

describe('more responses', () => {
  const upstream = (body: BodyInit | null, headers: Record<string, string>, status = 200) =>
    fetchMock.mockResolvedValue(new Response(body, { status, headers }));

  it('forces SVG (script-capable) to a sandboxed download', async () => {
    upstream('<svg onload="alert(1)"/>', { 'content-type': 'image/svg+xml' });
    const res = await GET(req('/api/proxy/api/x.svg'), ctx(['api', 'x.svg']));
    expect(res.headers.get('content-disposition')).toBe('attachment');
    expect(res.headers.get('content-security-policy')).toContain('sandbox');
  });

  it('keeps TTS audio inline', async () => {
    upstream('RIFF', { 'content-type': 'audio/wav' });
    const res = await POST(
      req('/api/proxy/api/tts/synthesize', undefined, { method: 'POST', body: '{}', headers: sameOrigin }),
      ctx(['api', 'tts', 'synthesize']),
    );
    expect(res.headers.get('content-type')).toBe('audio/wav');
    expect(res.headers.get('content-disposition')).toBeNull();
  });

  it('downloads a JSON file the orchestrator marks as an attachment', async () => {
    upstream('{"a":1}', { 'content-type': 'application/json', 'content-disposition': 'attachment; filename="x.json"' });
    const res = await GET(req('/api/proxy/api/documents/3/download'), ctx(['api', 'documents', '3', 'download']));
    expect(res.headers.get('content-disposition')).toBe('attachment; filename="x.json"');
  });

  it('does not forward the Location of a refused redirect', async () => {
    upstream(null, { location: 'https://evil.example/' }, 302);
    const res = await GET(req('/api/proxy/api/tasks'), ctx(['api', 'tasks']));
    expect(res.status).toBe(502);
    expect(res.headers.get('location')).toBeNull();
  });

  it('passes a 204 through', async () => {
    upstream(null, {}, 204);
    const res = await DELETE(
      req('/api/proxy/api/tasks/1', undefined, { method: 'DELETE', headers: sameOrigin }),
      ctx(['api', 'tasks', '1']),
    );
    expect(res.status).toBe(204);
  });

  it('attaches the API bearer and forwards multipart bytes unchanged', async () => {
    const form = '--b\r\nContent-Disposition: form-data; name="f"\r\n\r\n\u00ff\u00fe\r\n--b--\r\n';
    await POST(
      req('/api/proxy/api/documents', undefined, {
        method: 'POST',
        body: form,
        headers: { ...sameOrigin, 'content-type': 'multipart/form-data; boundary=b' },
      }),
      ctx(['api', 'documents']),
    );
    const [, opts] = fetchMock.mock.calls[0];
    expect(opts.headers.Authorization).toMatch(/^Bearer /);
    expect(Buffer.isBuffer(opts.body)).toBe(true);
    expect(opts.body.toString('utf8')).toBe(form);
  });

  it('allows writes without Sec-Fetch-Site or Origin (non-browser clients)', async () => {
    const res = await POST(req('/api/proxy/api/tasks', undefined, { method: 'POST', body: '{}' }), ctx(['api', 'tasks']));
    expect(res.status).toBe(200);
  });
});
