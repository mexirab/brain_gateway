import { NextRequest, NextResponse } from 'next/server';

const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL || 'http://localhost:8888';
const COOKIE_NAME = 'brain_token';
const TOKEN = process.env.DASHBOARD_TOKEN || 'changeme';
const API_TOKEN = process.env.API_TOKEN || '';

function isAuthed(request: NextRequest): boolean {
  return request.cookies.get(COOKIE_NAME)?.value === TOKEN;
}

/**
 * The first-boot setup wizard at /setup is reachable without dashboard auth
 * (the user has nothing to authenticate with yet on a fresh install).
 * Once setup_state.json marks setup_completed: true, the orchestrator's
 * /api/setup/* write endpoints return 410 — the kill switch lives on the
 * backend, so this proxy doesn't need to re-enforce it.
 */
function isSetupPath(request: NextRequest): boolean {
  return request.nextUrl.pathname.startsWith('/api/proxy/api/setup/');
}

/** Return binary response directly instead of parsing as JSON. */
function isBinaryResponse(res: Response): boolean {
  const ct = res.headers.get('content-type') || '';
  return ct.startsWith('audio/') || ct.startsWith('image/') || ct === 'application/octet-stream';
}

function binaryResponse(res: Response, ct: string): Response {
  return new Response(res.body, {
    status: res.status,
    headers: { 'Content-Type': ct },
  });
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  if (!isSetupPath(request) && !isAuthed(request)) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const { path } = await params;
  const targetPath = '/' + path.join('/');
  const url = new URL(targetPath, ORCHESTRATOR_URL);
  url.search = request.nextUrl.search;

  const res = await fetch(url.toString(), {
    headers: { 'Authorization': `Bearer ${API_TOKEN}` },
  });

  if (isBinaryResponse(res)) {
    return binaryResponse(res, res.headers.get('content-type')!);
  }

  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  if (!isSetupPath(request) && !isAuthed(request)) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const { path } = await params;
  const targetPath = '/' + path.join('/');
  const url = new URL(targetPath, ORCHESTRATOR_URL);

  const contentType = request.headers.get('content-type') || 'application/json';

  // Multipart form-data: forward raw bytes to preserve boundary
  const body = contentType.includes('multipart/')
    ? Buffer.from(await request.arrayBuffer())
    : await request.text();

  const res = await fetch(url.toString(), {
    method: 'POST',
    headers: { 'Content-Type': contentType, 'Authorization': `Bearer ${API_TOKEN}` },
    body,
  });

  // Handle streaming responses (SSE from chat completions)
  if (res.headers.get('content-type')?.includes('text/event-stream')) {
    return new Response(res.body, {
      status: res.status,
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      },
    });
  }

  // Handle binary responses (TTS audio)
  if (isBinaryResponse(res)) {
    return binaryResponse(res, res.headers.get('content-type')!);
  }

  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  if (!isSetupPath(request) && !isAuthed(request)) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const { path } = await params;
  const targetPath = '/' + path.join('/');
  const url = new URL(targetPath, ORCHESTRATOR_URL);

  const body = await request.text();
  const contentType = request.headers.get('content-type') || 'application/json';

  const res = await fetch(url.toString(), {
    method: 'PUT',
    headers: { 'Content-Type': contentType, 'Authorization': `Bearer ${API_TOKEN}` },
    body,
  });

  if (res.status === 204) return new Response(null, { status: 204 });
  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  if (!isSetupPath(request) && !isAuthed(request)) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const { path } = await params;
  const targetPath = '/' + path.join('/');
  const url = new URL(targetPath, ORCHESTRATOR_URL);

  const body = await request.text();
  const contentType = request.headers.get('content-type') || 'application/json';

  const res = await fetch(url.toString(), {
    method: 'PATCH',
    headers: { 'Content-Type': contentType, 'Authorization': `Bearer ${API_TOKEN}` },
    body,
  });

  if (res.status === 204) return new Response(null, { status: 204 });
  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  if (!isSetupPath(request) && !isAuthed(request)) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const { path } = await params;
  const targetPath = '/' + path.join('/');
  const url = new URL(targetPath, ORCHESTRATOR_URL);
  url.search = request.nextUrl.search;

  const res = await fetch(url.toString(), {
    method: 'DELETE',
    headers: { 'Authorization': `Bearer ${API_TOKEN}` },
  });
  if (res.status === 204) return new Response(null, { status: 204 });
  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}
