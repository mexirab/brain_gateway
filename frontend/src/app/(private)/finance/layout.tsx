'use client';

import { FinanceProvider } from '@/lib/finance-context';

export default function FinanceLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <FinanceProvider>{children}</FinanceProvider>;
}
