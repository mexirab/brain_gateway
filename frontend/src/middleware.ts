import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

import { COOKIE_NAME, dashboardSessionKind } from '@/lib/dashboard-auth';
import { kioskMayOpenPage } from '@/lib/kiosk-session';
import { PROTECTED_PATHS, isProtectedPath } from '@/lib/protected-paths';

// The protected route list lives in lib/protected-paths.ts (the login page uses
// it too). Re-exported for middleware.test.ts.
export { PROTECTED_PATHS };

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (!isProtectedPath(pathname)) return NextResponse.next();

  const session = await dashboardSessionKind(request.cookies.get(COOKIE_NAME)?.value);
  if (session === null) {
    const loginUrl = new URL('/login', request.url);
    // Keep the query (e.g. /pantry?tab=scan); the login page validates it.
    loginUrl.searchParams.set('redirect', pathname + request.nextUrl.search);
    return NextResponse.redirect(loginUrl);
  }

  // A kiosk (read-only display) session opens /dashboard and nothing else. 403, not a login
  // redirect: the kiosk is logged in, and sending it to /login would only make it set its cookie again.
  if (session === 'kiosk' && !kioskMayOpenPage(pathname)) {
    return new NextResponse('This screen can only show the dashboard.', {
      status: 403,
      headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' },
    });
  }

  return NextResponse.next();
}

export const config = {
  matcher: ['/dashboard/:path*', '/chat/:path*', '/tasks/:path*', '/home/:path*', '/finance/:path*', '/announcements/:path*', '/shopping/:path*', '/documents/:path*', '/architecture/:path*', '/settings/:path*', '/workouts/:path*', '/meals/:path*', '/pantry/:path*', '/personal-facts/:path*', '/calendar/:path*', '/avatar/:path*'],
};
