import { NextRequest, NextResponse } from 'next/server';
import { COOKIE_NAME, isValidDashboardSession } from '@/lib/dashboard-auth';
import { asAttachment } from '@/lib/content-disposition';
import { buildUpstreamUrl } from '@/lib/proxy-url';
import { isCrossSiteRequest } from '@/lib/same-origin';
import { logEvent, routeLabel } from '@/lib/server-log';

const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL || 'http://localhost:8888';
const API_TOKEN = process.env.API_TOKEN || '';

type Ctx = { params: Promise<{ path: string[] }> };
type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

// Upstream types the browser may render inline on the dashboard origin. Anything
// else (HTML, SVG, PDF, text from the document vault…) is forced to download, so
// an uploaded file can never run script as the dashboard.
const INLINE_SAFE = /^(image\/(png|jpe?g|gif|webp|avif)|audio\/[\w.+-]+)$/i;

function jsonError(error: string, status: number): NextResponse {
  return NextResponse.json({ error }, { status });
}

/** Stream a non-JSON upstream body back with only safe headers. */
function passthrough(res: Response, contentType: string): Response {
  const type = contentType.split(';')[0].trim();
  const headers: Record<string, string> = {
    'Content-Type': contentType,
    'X-Content-Type-Options': 'nosniff',
  };
  const disposition = res.headers.get('content-disposition');
  if (INLINE_SAFE.test(type)) {
    if (disposition) headers['Content-Disposition'] = disposition;
  } else {
    headers['Content-Disposition'] = asAttachment(disposition);
    headers['Content-Security-Policy'] = "sandbox; default-src 'none'";
  }
  // Pass through upstream caching (versioned exercise photos), but only
  // browser-private directives: this is a cookie-authed proxy, never let a
  // shared cache in front of the dashboard store it.
  const cache = res.headers.get('cache-control');
  if (cache && /\b(private|no-store)\b/i.test(cache) && !/\bpublic\b/i.test(cache)) {
    headers['Cache-Control'] = cache;
  }
  return new Response(res.body, { status: res.status, headers });
}

/** Turn the orchestrator's response into ours. */
async function relay(res: Response, route: string, method: Method): Promise<Response> {
  const bad = (reason: string, error: string, status = 502) => {
    logEvent('warning', '[PROXY] Bad upstream response', { reason, route, method, upstream_status: res.status });
    return jsonError(error, status);
  };
  if (res.status === 204) return new Response(null, { status: 204 });
  // Never follow or forward a redirect: it could point anywhere, and the
  // request that produced it carried the API token.
  if (res.status >= 300 && res.status < 400) return bad('redirect', 'Unexpected redirect from orchestrator');

  const contentType = res.headers.get('content-type') || '';
  // Streaming chat completions (SSE)
  if (contentType.includes('text/event-stream')) {
    return new Response(res.body, {
      status: res.status,
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
        'X-Content-Type-Options': 'nosniff',
      },
    });
  }
  const isAttachment = /^\s*attachment\b/i.test(res.headers.get('content-disposition') ?? '');
  if (contentType && (!contentType.includes('json') || (isAttachment && res.ok))) {
    // A plain-text/HTML error page (crashed handler, proxy in between) is not
    // a file: give the page a JSON error it can show.
    if (!res.ok) {
      return bad('non_json_error', `Orchestrator error (HTTP ${res.status})`, res.status >= 500 ? 502 : res.status);
    }
    return passthrough(res, contentType);
  }

  try {
    return NextResponse.json(await res.json(), { status: res.status });
  } catch {
    return bad('unreadable_json', `Orchestrator sent an unreadable response (HTTP ${res.status})`);
  }
}

async function forward(request: NextRequest, { params }: Ctx, method: Method): Promise<Response> {
  if (!(await isValidDashboardSession(request.cookies.get(COOKIE_NAME)?.value))) {
    return jsonError('Unauthorized', 401);
  }
  const { path } = await params;
  const route = routeLabel(path);
  if (method !== 'GET' && isCrossSiteRequest(request.headers)) {
    logEvent('warning', '[PROXY] Cross-site write refused', {
      route,
      method,
      sec_fetch_site: request.headers.get('sec-fetch-site'),
      has_origin: request.headers.has('origin'),
    });
    return jsonError('Cross-site request refused', 403);
  }

  const search = method === 'GET' || method === 'DELETE' ? request.nextUrl.search : '';
  const url = buildUpstreamUrl(path, search, ORCHESTRATOR_URL);
  if (!url) return jsonError('Path not allowed', 400);

  const headers: Record<string, string> = { Authorization: `Bearer ${API_TOKEN}` };
  let body: BodyInit | undefined;
  if (method === 'POST' || method === 'PUT' || method === 'PATCH') {
    const contentType = request.headers.get('content-type') || 'application/json';
    headers['Content-Type'] = contentType;
    // JSON/text as text; anything else (multipart boundaries, raw audio…) as
    // raw bytes, so a UTF-8 decode can't corrupt it.
    body = /^(application\/json|text\/)/i.test(contentType)
      ? await request.text()
      : Buffer.from(await request.arrayBuffer());
  }

  let res: Response;
  try {
    res = await fetch(url.toString(), { method, headers, body, redirect: 'manual' });
  } catch (e) {
    const code = (e as { cause?: { code?: unknown } })?.cause?.code;
    logEvent('warning', '[PROXY] Orchestrator unreachable', {
      route,
      method,
      code: typeof code === 'string' ? code : 'unknown',
    });
    return jsonError('Orchestrator unreachable', 502);
  }
  return relay(res, route, method);
}

export const GET = (request: NextRequest, ctx: Ctx) => forward(request, ctx, 'GET');
export const POST = (request: NextRequest, ctx: Ctx) => forward(request, ctx, 'POST');
export const PUT = (request: NextRequest, ctx: Ctx) => forward(request, ctx, 'PUT');
export const PATCH = (request: NextRequest, ctx: Ctx) => forward(request, ctx, 'PATCH');
export const DELETE = (request: NextRequest, ctx: Ctx) => forward(request, ctx, 'DELETE');
