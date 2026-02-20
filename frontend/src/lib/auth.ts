import { cookies } from 'next/headers';

const COOKIE_NAME = 'brain_token';
const TOKEN = process.env.DASHBOARD_TOKEN || 'changeme';

export function validateToken(token: string): boolean {
  return token === TOKEN;
}

export async function isAuthenticated(): Promise<boolean> {
  const cookieStore = await cookies();
  const token = cookieStore.get(COOKIE_NAME)?.value;
  return token ? validateToken(token) : false;
}

export { COOKIE_NAME };
