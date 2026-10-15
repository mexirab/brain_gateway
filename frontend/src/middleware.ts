import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

import { PROTECTED_PATHS, isProtectedPath } from '@/lib/protected-paths';

// The protected route list lives in lib/protected-paths.ts (the login page uses
// it too). Re-exported for middleware.test.ts.
export { PROTECTED_PATHS };

const COOKIE_NAME = 'brain_token';

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (!isProtectedPath(pathname)) return NextResponse.next();

  const token = request.cookies.get(COOKIE_NAME)?.value;
  const expectedToken = process.env.DASHBOARD_TOKEN || 'changeme';

  if (token !== expectedToken) {
    const loginUrl = new URL('/login', request.url);
    // Keep the query (e.g. /pantry?tab=scan); the login page validates it.
    loginUrl.searchParams.set('redirect', pathname + request.nextUrl.search);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  matcher: ['/dashboard/:path*', '/chat/:path*', '/tasks/:path*', '/home/:path*', '/finance/:path*', '/announcements/:path*', '/shopping/:path*', '/documents/:path*', '/architecture/:path*', '/settings/:path*', '/workouts/:path*', '/meals/:path*', '/pantry/:path*', '/personal-facts/:path*'],
};
