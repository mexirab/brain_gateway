import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

// /setup is intentionally NOT in this list — it's the first-boot wizard
// and must be reachable on a fresh install when no dashboard password
// has been configured. The orchestrator's /api/setup/* write endpoints
// flip to HTTP 410 after setup_state.json marks setup_completed: true,
// so the kill switch lives on the backend, not here.
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
