import Link from 'next/link';
import { LayoutDashboard, MessageSquare, Home, LogOut, Coins, Network, Volume2, ShoppingCart, FileText, Dumbbell, UtensilsCrossed, Settings } from 'lucide-react';
import MobileNav from '@/components/layout/MobileNav';

const NAV_ITEMS = [
  { href: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/announcements', label: 'Announcements', icon: Volume2 },
  { href: '/workouts', label: 'Workouts', icon: Dumbbell },
  { href: '/meals', label: 'Meals', icon: UtensilsCrossed },
  { href: '/shopping', label: 'Shopping', icon: ShoppingCart },
  { href: '/documents', label: 'Documents', icon: FileText },
  { href: '/finance', label: 'Finance', icon: Coins },
  { href: '/chat', label: 'Chat', icon: MessageSquare },
  { href: '/home', label: 'Home', icon: Home },
  { href: '/architecture', label: 'Architecture', icon: Network },
  { href: '/settings', label: 'Settings', icon: Settings },
];

export default function PrivateLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="min-h-screen flex">
      {/* Sidebar — hidden on mobile, shown on md+ */}
      <aside className="hidden md:flex flex-col w-56 bg-surface-raised border-r border-zinc-800 p-4">
        <Link href="/" className="text-lg font-bold text-brand-500 mb-8">
          Jess
        </Link>
        <nav className="flex-1 space-y-1">
          {NAV_ITEMS.map(({ href, label, icon: Icon }) => (
            <Link
              key={href}
              href={href}
              className="flex items-center gap-3 px-3 py-2 rounded-lg text-zinc-400 hover:text-white hover:bg-surface-overlay transition-colors"
            >
              <Icon size={18} />
              {label}
            </Link>
          ))}
        </nav>
        <form action="/api/auth/logout" method="POST">
          <button
            type="submit"
            className="flex items-center gap-3 px-3 py-2 rounded-lg text-zinc-500 hover:text-red-400 transition-colors w-full"
          >
            <LogOut size={18} />
            Sign Out
          </button>
        </form>
      </aside>

      {/* Mobile bottom nav (5 primary tabs + More sheet) */}
      <MobileNav />

      {/* Main content */}
      <main className="flex-1 p-6 pb-20 md:pb-6 overflow-auto">
        {children}
      </main>
    </div>
  );
}
