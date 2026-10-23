'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { FinanceProvider } from '@/lib/finance-context';
import { Swords, ScrollText, Shield, ArrowUpDown, Settings } from 'lucide-react';

const FINANCE_TABS = [
  { href: '/finance/quest-board', label: 'Quest Board', icon: Shield },
  { href: '/finance/side-quests', label: 'Side Quests', icon: ScrollText },
  { href: '/finance/boss-battle', label: 'Boss Battle', icon: Swords },
  { href: '/finance/transactions', label: 'Transactions', icon: ArrowUpDown },
  { href: '/finance/settings', label: 'Settings', icon: Settings },
];

export default function FinanceShell({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();

  return (
    <FinanceProvider>
      {/* Sub-navigation tabs */}
      <div className="max-w-4xl mx-auto mb-4">
        <div className="segmented">
          {FINANCE_TABS.map((tab) => {
            const active = pathname === tab.href;
            const Icon = tab.icon;
            return (
              <Link
                key={tab.href}
                href={tab.href}
                aria-current={active ? 'page' : undefined}
                aria-label={tab.label}
                className={`segmented-item px-2 sm:px-3 ${active ? 'segmented-item-active' : ''}`}
              >
                <Icon size={14} aria-hidden />
                <span className="hidden sm:inline">{tab.label}</span>
              </Link>
            );
          })}
        </div>
      </div>
      {children}
    </FinanceProvider>
  );
}
