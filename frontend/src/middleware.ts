import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

// All app routes require auth. `/architecture` is included deliberately: it now
// shows live infra status via the authenticated `/api/services` + `/health`
// proxy, so it must not be exposed to anonymous users. It lives under the
// `(private)` route group to match.
const PROTECTED_PATHS = ['/dashboard', '/chat', '/home', '/finance', '/announcements', '/shopping', '/documents', '/architecture', '/settings', '/workouts', '/meals'];
const COOKIE_NAME = 'brain_token';

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  const isProtected = PROTECTED_PATHS.some(
    (p) => pathname === p || pathname.startsWith(p + '/'),
  );

  if (!isProtected) return NextResponse.next();

  const token = request.cookies.get(COOKIE_NAME)?.value;
  const expectedToken = process.env.DASHBOARD_TOKEN || 'changeme';

  if (token !== expectedToken) {
    const loginUrl = new URL('/login', request.url);
    loginUrl.searchParams.set('redirect', pathname);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  matcher: ['/dashboard/:path*', '/chat/:path*', '/home/:path*', '/finance/:path*', '/announcements/:path*', '/shopping/:path*', '/documents/:path*', '/architecture/:path*', '/settings/:path*', '/workouts/:path*', '/meals/:path*'],
};
