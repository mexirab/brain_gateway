'use client';

import { Suspense, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { Loader2, ArrowUpDown, Tag, ChevronLeft, ChevronRight } from 'lucide-react';
import { useFinance } from '@/lib/finance-context';
import { financeApi } from '@/lib/finance-api';
import { formatCurrency, currentYearMonth } from '@/lib/finance-utils';
import { Card, ErrorState } from '@/components/ui';

type FilterType = 'all' | 'discretionary' | 'non-discretionary';

export default function TransactionsPage() {
  return (
    <Suspense fallback={<div className="flex items-center justify-center min-h-[60vh]"><Loader2 className="animate-spin text-brand" size={32} /></div>}>
      <TransactionsContent />
    </Suspense>
  );
}

function TransactionsContent() {
  const searchParams = useSearchParams();
  const initialFilter = (searchParams.get('filter') as FilterType) || 'all';
  const { transactions, loading, error, refresh } = useFinance();
  const [reclassifying, setReclassifying] = useState<number | null>(null);
  const [filter, setFilter] = useState<FilterType>(initialFilter);
  const [sourceFilter, setSourceFilter] = useState<'all' | 'ynab' | 'manual'>('all');

  const yearMonth = currentYearMonth();
  const [, year, month] = yearMonth.match(/^(\d{4})-(\d{2})$/) || [];
  const monthName = new Date(parseInt(year), parseInt(month) - 1).toLocaleDateString('en-US', {
    month: 'long',
    year: 'numeric',
  });

  async function handleReclassify(txnId: number, currentDiscretionary: boolean) {
    setReclassifying(txnId);
    try {
      await financeApi.reclassifyTransaction(txnId, !currentDiscretionary);
      await refresh();
    } catch (err) {
      console.error('Failed to reclassify:', err);
    } finally {
      setReclassifying(null);
    }
  }

  const filtered = transactions.filter((t) => {
    if (filter === 'discretionary' && !t.is_discretionary) return false;
    if (filter === 'non-discretionary' && t.is_discretionary) return false;
    if (sourceFilter === 'ynab' && t.source !== 'ynab') return false;
    if (sourceFilter === 'manual' && t.source !== 'manual') return false;
    return true;
  });

  const totalDiscretionary = transactions
    .filter((t) => t.is_discretionary)
    .reduce((sum, t) => sum + t.amount, 0);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <Loader2 className="animate-spin text-brand" size={32} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="max-w-4xl mx-auto mt-12">
        <ErrorState
          title="Couldn’t load transactions"
          message="We couldn’t reach your finance data. Check the connection and try again."
          onRetry={refresh}
        />
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-content-primary">Transactions</h1>
          <p className="text-sm text-content-muted mt-0.5">{monthName}</p>
        </div>
        <div className="text-right">
          <p className="text-lg font-bold text-brand font-mono">
            {formatCurrency(totalDiscretionary)}
          </p>
          <p className="text-xs text-content-muted">discretionary total</p>
        </div>
      </div>

      {/* Filters */}
      <div className="flex gap-2 flex-wrap">
        {/* Type filter */}
        <div className="flex gap-1 bg-surface-base/50 rounded-lg p-0.5 border border-line-subtle">
          {([
            { label: 'All', value: 'all' },
            { label: 'Discretionary', value: 'discretionary' },
            { label: 'Non-Disc.', value: 'non-discretionary' },
          ] as const).map((opt) => (
            <button
              key={opt.value}
              onClick={() => setFilter(opt.value)}
              className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                filter === opt.value
                  ? 'bg-surface-raised text-content-primary'
                  : 'text-content-muted hover:text-content-primary'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>

        {/* Source filter */}
        <div className="flex gap-1 bg-surface-base/50 rounded-lg p-0.5 border border-line-subtle">
          {([
            { label: 'All Sources', value: 'all' },
            { label: 'YNAB', value: 'ynab' },
            { label: 'Manual', value: 'manual' },
          ] as const).map((opt) => (
            <button
              key={opt.value}
              onClick={() => setSourceFilter(opt.value)}
              className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                sourceFilter === opt.value
                  ? 'bg-surface-raised text-content-primary'
                  : 'text-content-muted hover:text-content-primary'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>

        <span className="text-xs text-content-muted self-center ml-auto">
          {filtered.length} transaction{filtered.length !== 1 ? 's' : ''}
        </span>
      </div>

      {/* Transaction list */}
      {filtered.length === 0 ? (
        <Card padding="none" className="p-8 text-center">
          <ArrowUpDown size={32} className="text-content-muted mx-auto mb-3" />
          <p className="text-content-secondary text-sm">No transactions</p>
          <p className="text-content-muted text-xs mt-1">
            {transactions.length === 0
              ? 'Log expenses manually or sync from YNAB'
              : 'No transactions match the current filter'}
          </p>
        </Card>
      ) : (
        <Card padding="none" className="divide-y divide-line-subtle">
          {filtered.map((t) => (
            <div
              key={t.id}
              className="flex items-center gap-3 px-4 py-3 hover:bg-surface-raised/20 transition-colors"
            >
              {/* Discretionary indicator */}
              <button
                onClick={() => handleReclassify(t.id, Boolean(t.is_discretionary))}
                disabled={reclassifying === t.id}
                className={`flex-shrink-0 w-2.5 h-2.5 rounded-full transition-colors cursor-pointer ${
                  t.is_discretionary
                    ? 'bg-brand hover:bg-brand-hover'
                    : 'bg-surface-overlay hover:bg-line-strong'
                }`}
                title={
                  t.is_discretionary
                    ? 'Discretionary (click to toggle)'
                    : 'Non-discretionary (click to toggle)'
                }
              />

              {/* Transaction info */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <p className="text-sm text-content-primary truncate">
                    {t.name}
                  </p>
                  {t.source === 'ynab' && (
                    <span className="text-[10px] text-content-muted bg-surface-raised px-1.5 py-0.5 rounded uppercase tracking-wider flex-shrink-0">
                      YNAB
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2 mt-0.5">
                  <span className="text-xs text-content-muted">{t.date}</span>
                  {t.category && (
                    <span className="text-xs text-content-muted flex items-center gap-0.5">
                      <Tag size={9} />
                      {t.category}
                    </span>
                  )}
                </div>
              </div>

              {/* Amount */}
              <div className="text-right flex-shrink-0">
                <p
                  className={`text-sm font-mono ${
                    t.is_discretionary ? 'text-brand' : 'text-content-muted'
                  }`}
                >
                  {formatCurrency(t.amount)}
                </p>
              </div>

              {/* Reclassify loading */}
              {reclassifying === t.id && (
                <Loader2 size={14} className="animate-spin text-content-muted flex-shrink-0" />
              )}
            </div>
          ))}
        </Card>
      )}

      {/* Legend */}
      <div className="flex items-center gap-4 text-xs text-content-muted">
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-full bg-brand" />
          Discretionary (depletes health bar)
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-full bg-surface-overlay" />
          Non-discretionary
        </div>
        <span className="ml-auto">Click dot to reclassify</span>
      </div>
    </div>
  );
}
