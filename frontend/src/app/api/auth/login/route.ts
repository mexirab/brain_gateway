import { NextResponse } from 'next/server';
import { COOKIE_NAME, expectedDashboardToken } from '@/lib/dashboard-auth';

export async function POST(request: Request) {
  const token = expectedDashboardToken();
  if (token === null) {
    // Fail closed: no usable DASHBOARD_TOKEN means nobody can log in.
    console.error('[auth] DASHBOARD_TOKEN is unset or a placeholder; dashboard login disabled');
    return NextResponse.json(
      { error: 'Dashboard password not configured. Set DASHBOARD_TOKEN in .env and recreate the frontend.' },
      { status: 503 },
    );
  }

  let password: unknown;
  try {
    ({ password } = await request.json());
  } catch {
    return NextResponse.json({ error: 'Invalid request' }, { status: 400 });
  }

  if (password !== token) {
    return NextResponse.json({ error: 'Invalid password' }, { status: 401 });
  }

  const response = NextResponse.json({ ok: true });
  response.cookies.set(COOKIE_NAME, token, {
    httpOnly: true,
    secure: process.env.COOKIE_SECURE === 'true',
    sameSite: 'lax',
    path: '/',
    maxAge: 60 * 60 * 24 * 30, // 30 days
  });

  return response;
}
