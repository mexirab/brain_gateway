import { cookies } from 'next/headers';
import { COOKIE_NAME, isValidDashboardToken } from './dashboard-auth';

/** Server components: does this request carry a valid dashboard cookie? */
export async function isAuthenticated(): Promise<boolean> {
  const cookieStore = await cookies();
  return isValidDashboardToken(cookieStore.get(COOKIE_NAME)?.value);
}

export { COOKIE_NAME };
