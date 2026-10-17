import type { NextRequest } from 'next/server';

/** Secure cookies whenever the browser reached us over HTTPS (Tailscale Serve), or when forced. */
export function wantsSecureCookie(request: NextRequest): boolean {
  if (process.env.COOKIE_SECURE === 'true') return true;
  const proto = request.headers.get('x-forwarded-proto')?.split(',')[0].trim();
  return proto === 'https' || request.nextUrl.protocol === 'https:';
}
