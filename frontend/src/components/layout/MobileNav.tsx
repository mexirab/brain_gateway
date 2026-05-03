'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  LayoutDashboard,
  MessageSquare,
  Dumbbell,
  UtensilsCrossed,
  MoreHorizontal,
  ShoppingCart,
  FileText,
  Coins,
  Volume2,
  Home,
  Network,
  LogOut,
  Settings,
  X,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
}

const PRIMARY: NavItem[] = [
  { href: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/chat', label: 'Chat', icon: MessageSquare },
  { href: '/meals', label: 'Meals', icon: UtensilsCrossed },
  { href: '/workouts', label: 'Workouts', icon: Dumbbell },
];

const MORE: NavItem[] = [
  { href: '/shopping', label: 'Shopping', icon: ShoppingCart },
  { href: '/documents', label: 'Documents', icon: FileText },
  { href: '/finance', label: 'Finance', icon: Coins },
  { href: '/announcements', label: 'Announcements', icon: Volume2 },
  { href: '/home', label: 'Home', icon: Home },
  { href: '/architecture', label: 'Architecture', icon: Network },
  { href: '/settings', label: 'Settings', icon: Settings },
];

function isActive(pathname: string | null, href: string): boolean {
  if (!pathname) return false;
  return pathname === href || pathname.startsWith(href + '/');
}

export default function MobileNav() {
  const pathname = usePathname();
  const [moreOpen, setMoreOpen] = useState(false);
  const moreActive = MORE.some((item) => isActive(pathname, item.href));

  // Close the sheet on route change.
  useEffect(() => {
    setMoreOpen(false);
  }, [pathname]);

  // Lock body scroll + listen for Escape while the sheet is open.
  useEffect(() => {
    if (!moreOpen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMoreOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener('keydown', onKey);
    };
  }, [moreOpen]);

  return (
    <>
      <nav
        className="md:hidden fixed bottom-0 left-0 right-0 bg-surface-raised border-t border-zinc-800 flex z-50"
        style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
        aria-label="Primary"
      >
        {PRIMARY.map(({ href, label, icon: Icon }) => {
          const active = isActive(pathname, href);
          return (
            <Link
              key={href}
              href={href}
              aria-current={active ? 'page' : undefined}
              className={`relative flex-1 flex flex-col items-center justify-center gap-0.5 py-2 min-h-[56px] min-w-0 transition-colors ${
                active ? 'text-brand-500' : 'text-zinc-400 hover:text-white'
              }`}
            >
              {active && (
                <span
                  aria-hidden
                  className="absolute top-0 left-3 right-3 h-0.5 rounded-b bg-brand-500"
                />
              )}
              <Icon size={20} aria-hidden />
              <span className="text-[10px] leading-tight truncate max-w-full px-1">{label}</span>
            </Link>
          );
        })}
        <button
          type="button"
          onClick={() => setMoreOpen((v) => !v)}
          aria-haspopup="dialog"
          aria-expanded={moreOpen}
          aria-label="More navigation options"
          className={`relative flex-1 flex flex-col items-center justify-center gap-0.5 py-2 min-h-[56px] min-w-0 transition-colors ${
            moreOpen || moreActive ? 'text-brand-500' : 'text-zinc-400 hover:text-white'
          }`}
        >
          {(moreOpen || moreActive) && (
            <span
              aria-hidden
              className="absolute top-0 left-3 right-3 h-0.5 rounded-b bg-brand-500"
            />
          )}
          <MoreHorizontal size={20} aria-hidden />
          <span className="text-[10px] leading-tight">More</span>
        </button>
      </nav>

      {moreOpen && (
        <>
          <div
            aria-hidden
            onClick={() => setMoreOpen(false)}
            className="md:hidden fixed inset-0 z-40 bg-black/60 backdrop-blur-sm"
          />
          <div
            role="dialog"
            aria-modal="true"
            aria-label="More navigation"
            className="md:hidden fixed bottom-0 left-0 right-0 z-50 bg-surface-raised border-t border-zinc-800 rounded-t-2xl shadow-xl"
            style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
          >
            <div className="relative pt-3 pb-2">
              <div className="mx-auto h-1 w-10 rounded-full bg-zinc-700" aria-hidden />
              <button
                type="button"
                onClick={() => setMoreOpen(false)}
                aria-label="Close menu"
                className="absolute right-3 top-3 p-1 rounded-md text-zinc-500 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
              >
                <X size={18} aria-hidden />
              </button>
            </div>
            <div className="grid grid-cols-3 gap-2 px-4 pb-3">
              {MORE.map(({ href, label, icon: Icon }) => {
                const active = isActive(pathname, href);
                return (
                  <Link
                    key={href}
                    href={href}
                    aria-current={active ? 'page' : undefined}
                    className={`flex flex-col items-center justify-center gap-1 py-3 rounded-lg border transition-colors min-h-[64px] ${
                      active
                        ? 'border-brand-500/40 bg-brand-500/10 text-brand-500'
                        : 'border-zinc-800 text-zinc-300 hover:border-zinc-700 hover:bg-zinc-800/40'
                    }`}
                  >
                    <Icon size={20} aria-hidden />
                    <span className="text-xs">{label}</span>
                  </Link>
                );
              })}
            </div>
            <form action="/api/auth/logout" method="POST" className="px-4 pb-3">
              <button
                type="submit"
                className="w-full flex items-center justify-center gap-2 py-3 rounded-lg border border-zinc-800 text-zinc-400 hover:text-red-400 hover:border-red-500/30 transition-colors"
              >
                <LogOut size={16} aria-hidden />
                <span className="text-sm">Sign Out</span>
              </button>
            </form>
          </div>
        </>
      )}
    </>
  );
}
