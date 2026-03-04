'use client';

import { Suspense, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { Loader2, ArrowUpDown, Tag, ChevronLeft, ChevronRight } from 'lucide-react';
import { useFinance } from '@/lib/finance-context';
import { financeApi } from '@/lib/finance-api';
import { formatCurrency, currentYearMonth } from '@/lib/finance-utils';

type FilterType = 'all' | 'discretionary' | 'non-discretionary';

export default function TransactionsPage() {
  return (
    <Suspense fallback={<div className="flex items-center justify-center min-h-[60vh]"><Loader2 className="animate-spin text-brand-500" size={32} /></div>}>
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
        <Loader2 className="animate-spin text-brand-500" size={32} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="max-w-4xl mx-auto mt-12">
        <div className="glass p-6 border border-red-500/30 text-center">
          <p className="text-red-400 font-semibold">Failed to load transactions</p>
          <p className="text-sm text-zinc-500 mt-1">{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-zinc-100">Transactions</h1>
          <p className="text-sm text-zinc-500 mt-0.5">{monthName}</p>
        </div>
        <div className="text-right">
          <p className="text-lg font-bold text-brand-400 font-mono">
            {formatCurrency(totalDiscretionary)}
          </p>
          <p className="text-xs text-zinc-500">discretionary total</p>
        </div>
      </div>

      {/* Filters */}
      <div className="flex gap-2 flex-wrap">
        {/* Type filter */}
        <div className="flex gap-1 bg-zinc-900/50 rounded-lg p-0.5 border border-zinc-800">
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
                  ? 'bg-zinc-800 text-zinc-100'
                  : 'text-zinc-500 hover:text-zinc-300'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>

        {/* Source filter */}
        <div className="flex gap-1 bg-zinc-900/50 rounded-lg p-0.5 border border-zinc-800">
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
                  ? 'bg-zinc-800 text-zinc-100'
                  : 'text-zinc-500 hover:text-zinc-300'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>

        <span className="text-xs text-zinc-600 self-center ml-auto">
          {filtered.length} transaction{filtered.length !== 1 ? 's' : ''}
        </span>
      </div>

      {/* Transaction list */}
      {filtered.length === 0 ? (
        <div className="glass p-8 text-center">
          <ArrowUpDown size={32} className="text-zinc-600 mx-auto mb-3" />
          <p className="text-zinc-400 text-sm">No transactions</p>
          <p className="text-zinc-600 text-xs mt-1">
            {transactions.length === 0
              ? 'Log expenses manually or sync from YNAB'
              : 'No transactions match the current filter'}
          </p>
        </div>
      ) : (
        <div className="glass divide-y divide-zinc-800/50">
          {filtered.map((t) => (
            <div
              key={t.id}
              className="flex items-center gap-3 px-4 py-3 hover:bg-zinc-800/20 transition-colors"
            >
              {/* Discretionary indicator */}
              <button
                onClick={() => handleReclassify(t.id, Boolean(t.is_discretionary))}
                disabled={reclassifying === t.id}
                className={`flex-shrink-0 w-2.5 h-2.5 rounded-full transition-colors cursor-pointer ${
                  t.is_discretionary
                    ? 'bg-brand-500 hover:bg-brand-400'
                    : 'bg-zinc-700 hover:bg-zinc-600'
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
                  <p className="text-sm text-zinc-200 truncate">
                    {t.name}
                  </p>
                  {t.source === 'ynab' && (
                    <span className="text-[10px] text-zinc-600 bg-zinc-800 px-1.5 py-0.5 rounded uppercase tracking-wider flex-shrink-0">
                      YNAB
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2 mt-0.5">
                  <span className="text-xs text-zinc-500">{t.date}</span>
                  {t.category && (
                    <span className="text-xs text-zinc-600 flex items-center gap-0.5">
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
                    t.is_discretionary ? 'text-brand-400' : 'text-zinc-500'
                  }`}
                >
                  {formatCurrency(t.amount)}
                </p>
              </div>

              {/* Reclassify loading */}
              {reclassifying === t.id && (
                <Loader2 size={14} className="animate-spin text-zinc-500 flex-shrink-0" />
              )}
            </div>
          ))}
        </div>
      )}

      {/* Legend */}
      <div className="flex items-center gap-4 text-xs text-zinc-600">
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-full bg-brand-500" />
          Discretionary (depletes health bar)
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-full bg-zinc-700" />
          Non-discretionary
        </div>
        <span className="ml-auto">Click dot to reclassify</span>
      </div>
    </div>
  );
}
