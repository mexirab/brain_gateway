import { cookies } from 'next/headers';
import { COOKIE_NAME, dashboardSessionKind, type SessionKind } from './dashboard-auth';

/** Server components: which session does this request carry ('full', 'kiosk', or null)? */
export async function currentSession(): Promise<SessionKind | null> {
  const cookieStore = await cookies();
  return dashboardSessionKind(cookieStore.get(COOKIE_NAME)?.value);
}

export { COOKIE_NAME };
export type { SessionKind };
