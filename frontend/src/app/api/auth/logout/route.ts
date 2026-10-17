import { NextRequest, NextResponse } from 'next/server';
import { COOKIE_NAME } from '@/lib/dashboard-auth';
import { isCrossSiteRequest } from '@/lib/same-origin';
import { wantsSecureCookie } from '@/lib/secure-cookie';

export async function POST(request: NextRequest) {
  // A forged cross-site logout is only a nuisance, but there's no reason to allow it.
  if (isCrossSiteRequest(request.headers)) {
    return NextResponse.json({ error: 'Cross-site request refused' }, { status: 403 });
  }
  // The logout buttons are plain <form method=post>: send the browser back to
  // /login instead of leaving it on a raw {"ok":true} page.
  const wantsPage = (request.headers.get('accept') ?? '').includes('text/html');
  const response = wantsPage
    ? new NextResponse(null, { status: 303, headers: { Location: '/login' } }) // relative: request.url is the internal address behind Serve
    : NextResponse.json({ ok: true });
  response.cookies.set(COOKIE_NAME, '', {
    httpOnly: true,
    secure: wantsSecureCookie(request),
    sameSite: 'lax',
    path: '/',
    maxAge: 0,
  });
  return response;
}
