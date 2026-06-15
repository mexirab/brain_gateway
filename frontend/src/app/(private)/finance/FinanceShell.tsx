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
        <div className="flex gap-1 bg-zinc-900/50 rounded-xl p-1 border border-zinc-800">
          {FINANCE_TABS.map((tab) => {
            const active = pathname === tab.href;
            const Icon = tab.icon;
            return (
              <Link
                key={tab.href}
                href={tab.href}
                className={`flex items-center gap-1.5 px-2 sm:px-3 py-2 text-xs sm:text-sm font-medium rounded-lg transition-colors flex-1 justify-center ${
                  active
                    ? 'bg-zinc-800 text-zinc-100 shadow-sm'
                    : 'text-zinc-500 hover:text-zinc-300'
                }`}
              >
                <Icon size={14} />
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
