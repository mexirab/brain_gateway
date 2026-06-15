'use client';

import { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import {
  Swords, Calendar, Loader2, ScrollText, ChevronRight,
  RefreshCw, CheckCircle, CloudOff,
} from 'lucide-react';
import HealthBar from '@/components/finance/HealthBar';
import XPBar from '@/components/finance/XPBar';
import LevelBadge from '@/components/finance/LevelBadge';
import StreakCounter from '@/components/finance/StreakCounter';
import RetirementMeter from '@/components/finance/RetirementMeter';
import FutureSelfDamage from '@/components/finance/FutureSelfDamage';
import XPToast from '@/components/finance/XPToast';
import { WINDFALL_MONTHS, XP_AWARDS } from '@/lib/finance-constants';
import { formatCurrency, currentYearMonth } from '@/lib/finance-utils';
import { useFinance } from '@/lib/finance-context';
import { financeApi } from '@/lib/finance-api';
import { Card } from '@/components/ui';

export default function QuestBoardPage() {
  const {
    config,
    gameState,
    budget,
    transactions,
    sideQuests,
    loading,
    error,
    refresh,
    lastXPGain,
    clearXPGain,
  } = useFinance();

  const [syncing, setSyncing] = useState(false);
  const [lastSynced, setLastSynced] = useState<string | null>(null);
  const [ynabConnected, setYnabConnected] = useState(false);
  const [syncResult, setSyncResult] = useState<string | null>(null);

  const loadYnabStatus = useCallback(async () => {
    try {
      const status = await financeApi.getYnabStatus();
      setYnabConnected(status.connected);
      setLastSynced(status.last_synced_at);
    } catch {
      // YNAB status check failed — not critical
    }
  }, []);

  useEffect(() => {
    loadYnabStatus();
  }, [loadYnabStatus]);

  async function handleSync() {
    setSyncing(true);
    setSyncResult(null);
    try {
      const result = await financeApi.triggerYnabSync();
      if (result.error) {
        setSyncResult(`Error: ${result.error}`);
      } else {
        setSyncResult(`+${result.synced} transactions`);
        await refresh();
        await loadYnabStatus();
      }
    } catch {
      setSyncResult('Sync failed');
    } finally {
      setSyncing(false);
      setTimeout(() => setSyncResult(null), 4000);
    }
  }

  const yearMonth = currentYearMonth();
  const month = yearMonth.split('-')[1];
  const windfall = WINDFALL_MONTHS[month];
  const spent = budget?.discretionary_spent ?? 0;
  const budgetLimit = budget?.discretionary_budget ?? config.monthly_discretionary;
  const overspend = Math.max(0, spent - budgetLimit);

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
        <div className="glass p-6 border border-danger/30 text-center">
          <p className="text-danger font-semibold">Failed to load finance data</p>
          <p className="text-sm text-content-muted mt-1">{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* XP Toast */}
      {lastXPGain && (
        <XPToast
          amount={lastXPGain.amount}
          description={lastXPGain.description}
          onDismiss={clearXPGain}
        />
      )}

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-content-primary">Quest Board</h1>
          <p className="text-sm text-content-muted mt-0.5">
            {new Date().toLocaleDateString('en-US', { month: 'long', year: 'numeric' })}
          </p>
        </div>
      </div>

      {/* Top stats row: Level + Streak + XP */}
      <Card>
        <div className="flex flex-col sm:flex-row items-start sm:items-center gap-6">
          <LevelBadge level={gameState.level} />
          <div className="hidden sm:block w-px h-10 bg-line" />
          <StreakCounter months={gameState.streak_months} best={gameState.streak_best} />
          <div className="flex-1 min-w-0 w-full sm:w-auto">
            <XPBar totalXP={gameState.total_xp} level={gameState.level} />
          </div>
        </div>
      </Card>

      {/* Health Bar */}
      <HealthBar spent={spent} budget={budgetLimit} />

      {/* Future Self Damage (only shows when over budget) */}
      <FutureSelfDamage overspend={overspend} config={config} />

      {/* Boss Battle Banner (windfall months) */}
      {windfall && !budget?.boss_defeated && (
        <Link href="/finance/boss-battle" className="block">
          <div className="glass p-5 border-accent-gold/20 border hover:border-accent-gold/40 transition-colors cursor-pointer">
            <div className="flex items-center gap-3">
              <Swords size={24} className="text-accent-gold" />
              <div className="flex-1">
                <h3 className="text-sm font-semibold text-accent-gold">
                  Boss Battle Available
                </h3>
                <p className="text-sm text-content-secondary">
                  {windfall === 'bonus' ? 'Bonus' : 'ESPP'} windfall month — defeat the boss by splitting your windfall correctly!
                </p>
              </div>
              <ChevronRight size={18} className="text-accent-gold/50" />
            </div>
          </div>
        </Link>
      )}

      {/* Active Side Quests summary */}
      {sideQuests.filter(q => q.status === 'active').length > 0 && (
        <Link href="/finance/side-quests" className="block">
          <div className="glass p-4 hover:border-accent-violet/20 border border-transparent transition-colors cursor-pointer">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-xs font-semibold text-content-secondary uppercase tracking-wider flex items-center gap-1.5">
                <ScrollText size={12} />
                Active Side Quests
              </h3>
              <ChevronRight size={14} className="text-content-muted" />
            </div>
            <div className="space-y-2">
              {sideQuests.filter(q => q.status === 'active').slice(0, 3).map((q) => {
                const pct = q.target_amount > 0 ? Math.min(100, (q.saved_amount / q.target_amount) * 100) : 0;
                return (
                  <div key={q.id} className="flex items-center gap-3">
                    <span className="text-sm text-content-primary truncate flex-1">{q.name}</span>
                    <div className="w-20 h-1.5 bg-surface-raised rounded-full overflow-hidden">
                      <div
                        className="h-full bg-accent-violet rounded-full"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <span className="text-xs text-content-muted w-8 text-right">{Math.round(pct)}%</span>
                  </div>
                );
              })}
            </div>
          </div>
        </Link>
      )}

      {/* YNAB Sync + Retirement side by side */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* YNAB Sync Status + Recent Transactions */}
        <Card>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-content-primary uppercase tracking-wider">
              Spending Feed
            </h3>
            {ynabConnected ? (
              <div className="flex items-center gap-2">
                {syncResult && (
                  <span className={`text-xs ${syncResult.startsWith('Error') || syncResult.includes('failed') ? 'text-danger' : 'text-success'}`}>
                    {syncResult}
                  </span>
                )}
                <button
                  onClick={handleSync}
                  disabled={syncing}
                  className="flex items-center gap-1 px-2 py-1 text-xs text-content-secondary hover:text-content-primary bg-surface-raised hover:bg-surface-overlay rounded-md transition-colors disabled:opacity-50"
                  title="Sync from YNAB"
                >
                  {syncing ? (
                    <Loader2 size={12} className="animate-spin" />
                  ) : (
                    <RefreshCw size={12} />
                  )}
                  Sync
                </button>
              </div>
            ) : (
              <Link href="/finance/settings" className="flex items-center gap-1 text-xs text-content-muted hover:text-content-secondary">
                <CloudOff size={12} />
                Connect YNAB
              </Link>
            )}
          </div>

          {/* Sync info */}
          {ynabConnected && lastSynced && (
            <div className="flex items-center gap-1.5 mb-3 text-xs text-content-muted">
              <CheckCircle size={10} className="text-success/60" />
              Last sync: {new Date(lastSynced).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}
            </div>
          )}

          {/* Recent transactions */}
          {transactions.length > 0 ? (
            <div className="space-y-1.5">
              {transactions.slice(0, 6).map((t) => (
                <div key={t.id} className="flex items-center justify-between text-sm">
                  <div className="flex items-center gap-2 min-w-0 flex-1">
                    <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${t.is_discretionary ? 'bg-brand' : 'bg-surface-overlay'}`} />
                    <span className="text-content-secondary truncate">{t.name}</span>
                  </div>
                  <span className={`font-mono flex-shrink-0 ml-2 ${t.is_discretionary ? 'text-content-primary' : 'text-content-muted'}`}>
                    {formatCurrency(t.amount)}
                  </span>
                </div>
              ))}
              <Link
                href="/finance/transactions"
                className="block text-xs text-content-muted hover:text-brand mt-2 transition-colors"
              >
                View all {transactions.length} transactions →
              </Link>
            </div>
          ) : (
            <p className="text-sm text-content-muted text-center py-4">
              {ynabConnected ? 'No transactions yet — hit Sync' : 'Connect YNAB to auto-track spending'}
            </p>
          )}
        </Card>

        {/* Retirement Meter */}
        <RetirementMeter config={config} />
      </div>

      {/* Monthly Plan Summary */}
      <Card>
        <h3 className="text-sm font-semibold text-content-primary uppercase tracking-wider mb-3 flex items-center gap-2">
          <Calendar size={16} />
          Monthly Plan
        </h3>
        <div className="grid grid-cols-3 gap-4 text-center">
          <div>
            <p className="text-lg font-bold text-success">{formatCurrency(config.monthly_discretionary)}</p>
            <p className="text-xs text-content-muted">Guilt-Free</p>
          </div>
          <div>
            <p className="text-lg font-bold text-brand">{formatCurrency(config.monthly_investing)}</p>
            <p className="text-xs text-content-muted">Investing</p>
          </div>
          <div>
            <p className="text-lg font-bold text-content-secondary">{formatCurrency(config.monthly_buffer)}</p>
            <p className="text-xs text-content-muted">Buffer</p>
          </div>
        </div>
      </Card>

      {/* XP Legend */}
      <Card>
        <h3 className="text-sm font-semibold text-content-primary uppercase tracking-wider mb-3">
          How to Earn XP
        </h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
          <div className="flex justify-between text-content-secondary">
            <span>Stay under budget</span>
            <span className="text-accent-gold font-mono">+{XP_AWARDS.budget_under}</span>
          </div>
          <div className="flex justify-between text-content-secondary">
            <span>Monthly investment</span>
            <span className="text-accent-gold font-mono">+{XP_AWARDS.investment_transfer}</span>
          </div>
          <div className="flex justify-between text-content-secondary">
            <span>Windfall split</span>
            <span className="text-accent-gold font-mono">+{XP_AWARDS.boss_defeated}</span>
          </div>
          <div className="flex justify-between text-content-secondary">
            <span>Side quest complete</span>
            <span className="text-accent-gold font-mono">+{XP_AWARDS.side_quest_complete}</span>
          </div>
          <div className="flex justify-between text-content-secondary">
            <span>Quarterly review</span>
            <span className="text-accent-gold font-mono">+{XP_AWARDS.quarterly_review}</span>
          </div>
          <div className="flex justify-between text-content-secondary">
            <span>3-month streak bonus</span>
            <span className="text-accent-gold font-mono">+{XP_AWARDS.streak_milestone}</span>
          </div>
        </div>
      </Card>
    </div>
  );
}
