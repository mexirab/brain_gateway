import { NextRequest, NextResponse } from 'next/server';
import { COOKIE_NAME, expectedSessionValue, isCorrectPassword } from '@/lib/dashboard-auth';
import { beginAttempt, clientKey, recordSuccess } from '@/lib/login-rate-limit';
import { isCrossSiteRequest } from '@/lib/same-origin';
import { wantsSecureCookie } from '@/lib/secure-cookie';
import { logEvent } from '@/lib/server-log';

export async function POST(request: NextRequest) {
  const session = await expectedSessionValue();
  if (session === null) {
    // Fail closed: no usable DASHBOARD_TOKEN means nobody can log in.
    console.error('[auth] DASHBOARD_TOKEN is unset or a placeholder; dashboard login disabled');
    return NextResponse.json(
      { error: 'Dashboard password not configured. Set DASHBOARD_TOKEN in .env and recreate the frontend.' },
      { status: 503 },
    );
  }
  if (isCrossSiteRequest(request.headers)) {
    logEvent('warning', '[AUTH] Cross-site login refused', {
      sec_fetch_site: request.headers.get('sec-fetch-site'),
      has_origin: request.headers.has('origin'),
    });
    return NextResponse.json({ error: 'Cross-site request refused' }, { status: 403 });
  }

  const key = clientKey(request.headers);
  const wait = beginAttempt(key); // counted now; undone on success
  if (wait > 0) {
    logEvent('warning', '[AUTH] Login rate-limited', { retry_after_s: wait });
    return NextResponse.json(
      { error: 'Too many attempts. Try again later.' },
      { status: 429, headers: { 'Retry-After': String(wait) } },
    );
  }

  let password: unknown;
  try {
    ({ password } = await request.json());
  } catch {
    return NextResponse.json({ error: 'Invalid request' }, { status: 400 });
  }

  if (!(await isCorrectPassword(password))) {
    logEvent('warning', '[AUTH] Failed login');
    return NextResponse.json({ error: 'Invalid password' }, { status: 401 });
  }
  recordSuccess(key);

  const response = NextResponse.json({ ok: true });
  response.cookies.set(COOKIE_NAME, session, {
    httpOnly: true,
    secure: wantsSecureCookie(request),
    sameSite: 'lax',
    path: '/',
    maxAge: 60 * 60 * 24 * 30, // 30 days
  });
  return response;
}
