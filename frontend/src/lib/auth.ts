import { cookies } from 'next/headers';
import { COOKIE_NAME, isValidDashboardSession } from './dashboard-auth';

/** Server components: does this request carry a valid dashboard cookie? */
export async function isAuthenticated(): Promise<boolean> {
  const cookieStore = await cookies();
  return isValidDashboardSession(cookieStore.get(COOKIE_NAME)?.value);
}

export { COOKIE_NAME };
