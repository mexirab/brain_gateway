import Link from 'next/link';
import { redirect } from 'next/navigation';
import { LayoutDashboard, MessageSquare, Home, LogOut, Coins, Network, Volume2, ShoppingCart, FileText, Dumbbell, UtensilsCrossed, Refrigerator, Settings, ListTodo, Pill } from 'lucide-react';
import MobileNav from '@/components/layout/MobileNav';
import { SWRProvider } from '@/components/providers/SWRProvider';
import { isAuthenticated } from '@/lib/auth';
import { getFeatureFlags } from '@/lib/features.server';
import { isNavItemEnabled } from '@/lib/features';

const NAV_ITEMS = [
  { href: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/tasks', label: 'Tasks', icon: ListTodo },
  { href: '/announcements', label: 'Announcements', icon: Volume2 },
  { href: '/workouts', label: 'Workouts', icon: Dumbbell },
  { href: '/meals', label: 'Meals', icon: UtensilsCrossed },
  { href: '/pantry', label: 'Pantry', icon: Refrigerator },
  { href: '/shopping', label: 'Shopping', icon: ShoppingCart },
  { href: '/documents', label: 'Documents', icon: FileText },
  { href: '/finance', label: 'Finance', icon: Coins },
  { href: '/chat', label: 'Chat', icon: MessageSquare },
  { href: '/home', label: 'Home', icon: Home },
  { href: '/architecture', label: 'Architecture', icon: Network },
  { href: '/personal-facts', label: 'Facts', icon: Pill },
  { href: '/settings', label: 'Settings', icon: Settings },
];

export default async function PrivateLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Defense in depth: middleware.ts is the real gate, but a page added under
  // (private)/ without a PROTECTED_PATHS/matcher entry still fails closed here
  // (minus the ?redirect= middleware would add — the layout can't see the path).
  if (!(await isAuthenticated())) redirect('/login');

  const flags = await getFeatureFlags();
  const navItems = NAV_ITEMS.filter(({ href }) => isNavItemEnabled(href, flags));

  return (
    <div className="min-h-screen supports-[height:100dvh]:min-h-dvh flex">
      {/* Sidebar — hidden on mobile, shown on md+ */}
      <aside className="hidden md:flex flex-col w-56 bg-surface-raised border-r border-line-subtle p-4">
        <Link href="/" className="text-lg font-bold text-brand-500 mb-8">
          Jess
        </Link>
        <nav className="flex-1 space-y-1">
          {navItems.map(({ href, label, icon: Icon }) => (
            <Link
              key={href}
              href={href}
              className="flex items-center gap-3 px-3 py-2 rounded-lg text-content-secondary hover:text-white hover:bg-surface-overlay transition-colors"
            >
              <Icon size={18} />
              {label}
            </Link>
          ))}
        </nav>
        <form action="/api/auth/logout" method="POST">
          <button
            type="submit"
            className="flex items-center gap-3 px-3 py-2 rounded-lg text-content-muted hover:text-danger transition-colors w-full"
          >
            <LogOut size={18} />
            Sign Out
          </button>
        </form>
      </aside>

      {/* Mobile bottom nav (5 primary tabs + More sheet) */}
      <MobileNav flags={flags} />

      {/* Main content — mobile pb clears the fixed MobileNav. If you change
          this padding, update .page-viewport in globals.css to match. */}
      <main className="flex-1 p-6 pb-[calc(5rem+env(safe-area-inset-bottom,0px))] md:pb-6 overflow-auto">
        <SWRProvider>{children}</SWRProvider>
      </main>
    </div>
  );
}
