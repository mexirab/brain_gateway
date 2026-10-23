'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { LogOut } from 'lucide-react';
import { NAV_ITEMS, isNavActive } from '@/lib/nav';
import { isNavItemEnabled, type FeatureFlags } from '@/lib/features';
import ChatNavBadge from './ChatNavBadge';

/** Desktop (md+) navigation rail. Client component so it can show the current page. */
export default function Sidebar({ flags, appName }: { flags: FeatureFlags; appName: string }) {
  const pathname = usePathname();
  const items = NAV_ITEMS.filter(({ href }) => isNavItemEnabled(href, flags));

  return (
    <aside className="hidden md:flex flex-col w-56 shrink-0 bg-surface-raised/80 backdrop-blur-xl border-r border-line-subtle p-4 sticky top-0 h-screen supports-[height:100dvh]:h-dvh">
      <Link
        href="/"
        className="flex items-center gap-2 px-2 mb-8 text-lg font-bold tracking-tight text-content-primary"
      >
        <span
          aria-hidden
          className="grid h-7 w-7 place-items-center rounded-lg bg-brand text-sm text-on-brand shadow-glow"
        >
          {appName.charAt(0).toUpperCase()}
        </span>
        <span className="truncate">{appName}</span>
      </Link>
      <nav className="flex-1 space-y-0.5 overflow-y-auto" aria-label="Primary">
        {items.map(({ href, label, icon: Icon }) => {
          const active = isNavActive(pathname, href);
          return (
            <Link
              key={href}
              href={href}
              aria-current={active ? 'page' : undefined}
              className={`flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50 ${
                active
                  ? 'bg-brand/10 text-brand font-medium'
                  : 'text-content-secondary hover:text-content-primary hover:bg-surface-overlay'
              }`}
            >
              <Icon size={18} aria-hidden />
              {label}
              {href === '/chat' && <ChatNavBadge className="ml-auto" />}
            </Link>
          );
        })}
      </nav>
      <form action="/api/auth/logout" method="POST">
        <button
          type="submit"
          className="flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-content-muted hover:text-danger transition-colors w-full"
        >
          <LogOut size={18} aria-hidden />
          Sign Out
        </button>
      </form>
    </aside>
  );
}
