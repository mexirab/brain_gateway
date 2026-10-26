'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { LogOut, MoreHorizontal, X } from 'lucide-react';
import { isNavItemEnabled, type FeatureFlags } from '@/lib/features';
import { NAV_ITEMS, isNavActive as isActive } from '@/lib/nav';
import ChatNavBadge from './ChatNavBadge';

export default function MobileNav({ flags }: { flags: FeatureFlags }) {
  const pathname = usePathname();
  const [moreOpen, setMoreOpen] = useState(false);

  const enabled = NAV_ITEMS.filter((item) => isNavItemEnabled(item.href, flags));
  const primary = enabled.filter((item) => item.primary);
  const more = enabled.filter((item) => !item.primary);
  const moreActive = more.some((item) => isActive(pathname, item.href));

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
        className="md:hidden fixed bottom-0 left-0 right-0 bg-surface-raised/80 backdrop-blur-xl backdrop-saturate-150 border-t border-line-subtle flex z-50"
        style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
        aria-label="Primary"
      >
        {primary.map(({ href, label, icon: Icon }) => {
          const active = isActive(pathname, href);
          return (
            <Link
              key={href}
              href={href}
              aria-current={active ? 'page' : undefined}
              className={`relative flex-1 flex flex-col items-center justify-center gap-0.5 py-2 min-h-[56px] min-w-0 transition-colors ${
                active ? 'text-brand-500' : 'text-content-secondary hover:text-content-primary'
              }`}
            >
              {active && (
                <span
                  aria-hidden
                  className="absolute top-0 left-3 right-3 h-0.5 rounded-b bg-brand-500"
                />
              )}
              <span className="relative">
                <Icon size={20} aria-hidden />
                {href === '/chat' && <ChatNavBadge className="absolute -top-1.5 -right-2.5" />}
              </span>
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
            moreOpen || moreActive ? 'text-brand-500' : 'text-content-secondary hover:text-content-primary'
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
            className="md:hidden fixed inset-0 z-40 bg-black/60 backdrop-blur-sm animate-scrim-in"
          />
          <div
            role="dialog"
            aria-modal="true"
            aria-label="More navigation"
            className="md:hidden fixed bottom-0 left-0 right-0 z-50 bg-surface-raised border-t border-line-subtle rounded-t-[20px] shadow-xl animate-sheet-up"
            style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
          >
            <div className="relative pt-3 pb-2">
              <div className="mx-auto h-1 w-10 rounded-full bg-ink/20" aria-hidden />
              <button
                type="button"
                onClick={() => setMoreOpen(false)}
                aria-label="Close menu"
                className="absolute right-3 top-3 p-1 rounded-md text-content-muted hover:text-content-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
              >
                <X size={18} aria-hidden />
              </button>
            </div>
            <div className="grid grid-cols-3 gap-2 px-4 pb-3">
              {more.map(({ href, label, icon: Icon }) => {
                const active = isActive(pathname, href);
                return (
                  <Link
                    key={href}
                    href={href}
                    aria-current={active ? 'page' : undefined}
                    className={`flex flex-col items-center justify-center gap-1 py-3 rounded-xl border transition-colors active:scale-95 min-h-[64px] ${
                      active
                        ? 'border-brand-500/40 bg-brand-500/10 text-brand-500'
                        : 'border-line-subtle bg-surface-overlay/40 text-content-primary hover:border-line hover:bg-surface-overlay'
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
                className="w-full flex items-center justify-center gap-2 py-3 rounded-lg border border-line-subtle text-content-secondary hover:text-danger hover:border-danger/30 transition-colors"
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
