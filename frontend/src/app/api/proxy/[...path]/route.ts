import { NextRequest, NextResponse } from 'next/server';

const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL || 'http://localhost:8888';
const COOKIE_NAME = 'brain_token';
const TOKEN = process.env.DASHBOARD_TOKEN || 'changeme';

function isAuthed(request: NextRequest): boolean {
  return request.cookies.get(COOKIE_NAME)?.value === TOKEN;
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  if (!isAuthed(request)) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const { path } = await params;
  const targetPath = '/' + path.join('/');
  const url = new URL(targetPath, ORCHESTRATOR_URL);
  url.search = request.nextUrl.search;

  const res = await fetch(url.toString());
  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  if (!isAuthed(request)) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const { path } = await params;
  const targetPath = '/' + path.join('/');
  const url = new URL(targetPath, ORCHESTRATOR_URL);

  const body = await request.text();
  const contentType = request.headers.get('content-type') || 'application/json';

  const res = await fetch(url.toString(), {
    method: 'POST',
    headers: { 'Content-Type': contentType },
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

  const data = await res.json();
  return NextResponse.json(data, { status: res.status });
}
