import { redirect } from 'next/navigation';
import MobileNav from '@/components/layout/MobileNav';
import Sidebar from '@/components/layout/Sidebar';
import SessionExpiredWatcher from '@/components/providers/SessionExpiredWatcher';
import ServiceWorkerRegistrar from '@/components/providers/ServiceWorkerRegistrar';
import { SWRProvider } from '@/components/providers/SWRProvider';
import { isAuthenticated } from '@/lib/auth';
import { getAssistantName } from '@/lib/branding.server';
import { getFeatureFlags } from '@/lib/features.server';

export default async function PrivateLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Defense in depth: middleware.ts is the real gate, but a page added under
  // (private)/ without a PROTECTED_PATHS/matcher entry still fails closed here
  // (minus the ?redirect= middleware would add — the layout can't see the path).
  if (!(await isAuthenticated())) redirect('/login');

  const [flags, appName] = await Promise.all([getFeatureFlags(), getAssistantName()]);

  return (
    // SWRProvider wraps the nav too, so the Chat badge gets the app's
    // bounded retry/dedupe defaults, not SWR's unlimited retries.
    <SWRProvider>
    <div className="min-h-screen supports-[height:100dvh]:min-h-dvh flex">
      {/* Sidebar — hidden on mobile, shown on md+ */}
      <Sidebar flags={flags} appName={appName} />

      {/* Mobile bottom nav (5 primary tabs + More sheet) */}
      <MobileNav flags={flags} />

      {/* Main content — mobile pt clears the status bar (viewportFit: cover)
          and pb the fixed MobileNav. If you change this padding, update
          .page-viewport in globals.css to match. */}
      <main className="flex-1 min-w-0 p-6 pl-[max(1.5rem,env(safe-area-inset-left,0px))] pr-[max(1.5rem,env(safe-area-inset-right,0px))] pt-[calc(1.5rem+env(safe-area-inset-top,0px))] pb-[calc(5rem+env(safe-area-inset-bottom,0px))] md:pl-6 md:pb-[calc(1.5rem+env(safe-area-inset-bottom,0px))] overflow-auto">
        <SessionExpiredWatcher />
        <ServiceWorkerRegistrar />
        {children}
      </main>
    </div>
    </SWRProvider>
  );
}
