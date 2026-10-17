import { Card } from '@/components/ui';
import { expectedDashboardToken } from '@/lib/dashboard-auth';
import LoginForm from './LoginForm';

// Read DASHBOARD_TOKEN at request time, so "not configured" shows before anyone types.
export const dynamic = 'force-dynamic';

export default function LoginPage() {
  return (
    <main className="min-h-screen flex items-center justify-center px-6">
      <Card padding="none" className="p-8 w-full max-w-sm space-y-6">
        <div className="text-center">
          <h1 className="text-2xl font-bold">Convivial Prophet</h1>
          <p className="text-content-secondary mt-1">Enter your password</p>
        </div>
        <LoginForm configured={expectedDashboardToken() !== null} />
      </Card>
    </main>
  );
}
