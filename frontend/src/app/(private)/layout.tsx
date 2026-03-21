import Link from 'next/link';
import { LayoutDashboard, MessageSquare, Home, LogOut, Coins, Network, Volume2, ShoppingCart } from 'lucide-react';

const NAV_ITEMS = [
  { href: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/announcements', label: 'Announcements', icon: Volume2 },
  { href: '/shopping', label: 'Shopping', icon: ShoppingCart },
  { href: '/finance', label: 'Finance', icon: Coins },
  { href: '/chat', label: 'Chat', icon: MessageSquare },
  { href: '/home', label: 'Home', icon: Home },
  { href: '/architecture', label: 'Architecture', icon: Network },
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

      {/* Mobile bottom nav */}
      <nav className="md:hidden fixed bottom-0 left-0 right-0 bg-surface-raised border-t border-zinc-800 flex z-50">
        {NAV_ITEMS.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className="flex-1 flex flex-col items-center gap-1 py-3 text-zinc-400 hover:text-white transition-colors"
          >
            <Icon size={20} />
            <span className="text-xs">{label}</span>
          </Link>
        ))}
      </nav>

      {/* Main content */}
      <main className="flex-1 p-6 pb-20 md:pb-6 overflow-auto">
        {children}
      </main>
    </div>
  );
}
