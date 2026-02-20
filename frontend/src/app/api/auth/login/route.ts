import { NextResponse } from 'next/server';

const COOKIE_NAME = 'brain_token';
const TOKEN = process.env.DASHBOARD_TOKEN || 'changeme';

export async function POST(request: Request) {
  const body = await request.json();
  const { password } = body;

  if (password !== TOKEN) {
    return NextResponse.json({ error: 'Invalid password' }, { status: 401 });
  }

  const response = NextResponse.json({ ok: true });
  response.cookies.set(COOKIE_NAME, TOKEN, {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    path: '/',
    maxAge: 60 * 60 * 24 * 30, // 30 days
  });

  return response;
}
