import { Card } from '@/components/ui';
import { getAssistantName } from '@/lib/branding.server';
import { expectedDashboardToken } from '@/lib/dashboard-auth';
import LoginForm from './LoginForm';

// Read DASHBOARD_TOKEN at request time, so "not configured" shows before anyone types.
export const dynamic = 'force-dynamic';

export default async function LoginPage() {
  const name = await getAssistantName();
  return (
    <main className="min-h-screen flex items-center justify-center px-6 pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)]">
      <Card padding="none" className="p-8 w-full max-w-sm space-y-6">
        <div className="flex flex-col items-center text-center">
          <span
            aria-hidden
            className="mb-4 grid h-14 w-14 place-items-center rounded-2xl bg-brand text-2xl font-bold text-on-brand shadow-glow"
          >
            {name.charAt(0).toUpperCase()}
          </span>
          <h1 className="text-2xl font-bold tracking-tight">{name}</h1>
          <p className="text-content-secondary mt-1">Enter your password</p>
        </div>
        <LoginForm configured={expectedDashboardToken() !== null} />
      </Card>
    </main>
  );
}
