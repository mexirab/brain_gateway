import { NextResponse } from 'next/server';
import { COOKIE_NAME } from '@/lib/dashboard-auth';

export async function POST() {
  const response = NextResponse.json({ ok: true });
  response.cookies.set(COOKIE_NAME, '', {
    httpOnly: true,
    secure: process.env.COOKIE_SECURE === 'true',
    sameSite: 'lax',
    path: '/',
    maxAge: 0,
  });
  return response;
}
