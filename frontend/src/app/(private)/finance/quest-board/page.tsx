'use client';

import { useState } from 'react';
import { Swords, Calendar, Loader2 } from 'lucide-react';
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

export default function QuestBoardPage() {
  const {
    config,
    gameState,
    budget,
    transactions,
    loading,
    error,
    addExpense,
    lastXPGain,
    clearXPGain,
  } = useFinance();

  const [manualAmount, setManualAmount] = useState('');
  const [manualName, setManualName] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const yearMonth = currentYearMonth();
  const month = yearMonth.split('-')[1];
  const windfall = WINDFALL_MONTHS[month];
  const spent = budget?.discretionary_spent ?? 0;
  const budgetLimit = budget?.discretionary_budget ?? config.monthly_discretionary;
  const overspend = Math.max(0, spent - budgetLimit);

  async function handleAddExpense() {
    const amount = parseFloat(manualAmount);
    if (isNaN(amount) || amount <= 0) return;
    const name = manualName.trim() || 'Expense';
    setSubmitting(true);
    try {
      await addExpense(amount, name);
      setManualAmount('');
      setManualName('');
    } catch (err) {
      console.error('Failed to add expense:', err);
    } finally {
      setSubmitting(false);
    }
  }

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
          <p className="text-red-400 font-semibold">Failed to load finance data</p>
          <p className="text-sm text-zinc-500 mt-1">{error}</p>
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
          <h1 className="text-2xl font-bold text-zinc-100">Quest Board</h1>
          <p className="text-sm text-zinc-500 mt-0.5">
            {new Date().toLocaleDateString('en-US', { month: 'long', year: 'numeric' })}
          </p>
        </div>
      </div>

      {/* Top stats row: Level + Streak + XP */}
      <div className="glass p-5">
        <div className="flex flex-col sm:flex-row items-start sm:items-center gap-6">
          <LevelBadge level={gameState.level} />
          <div className="hidden sm:block w-px h-10 bg-zinc-700" />
          <StreakCounter months={gameState.streak_months} best={gameState.streak_best} />
          <div className="flex-1 min-w-0 w-full sm:w-auto">
            <XPBar totalXP={gameState.total_xp} level={gameState.level} />
          </div>
        </div>
      </div>

      {/* Health Bar */}
      <HealthBar spent={spent} budget={budgetLimit} />

      {/* Future Self Damage (only shows when over budget) */}
      <FutureSelfDamage overspend={overspend} config={config} />

      {/* Boss Battle Banner (windfall months) */}
      {windfall && (
        <div className="glass p-5 border-amber-500/20 border">
          <div className="flex items-center gap-3">
            <Swords size={24} className="text-amber-400" />
            <div>
              <h3 className="text-sm font-semibold text-amber-400">
                Boss Battle Available
              </h3>
              <p className="text-sm text-zinc-400">
                {windfall === 'bonus' ? 'Bonus' : 'ESPP'} windfall month — defeat the boss by splitting your windfall correctly!
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Manual Entry + Retirement side by side */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Quick Expense Entry */}
        <div className="glass p-5">
          <h3 className="text-sm font-semibold text-zinc-300 uppercase tracking-wider mb-3">
            Log Expense
          </h3>
          <div className="space-y-2">
            <input
              type="text"
              placeholder="What was it?"
              value={manualName}
              onChange={(e) => setManualName(e.target.value)}
              className="w-full bg-zinc-800 text-zinc-200 text-sm rounded-lg px-3 py-2 border border-zinc-700 focus:border-brand-500 focus:outline-none"
            />
            <div className="flex gap-2">
              <div className="relative flex-1">
                <span className="absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500 text-sm">$</span>
                <input
                  type="number"
                  placeholder="0"
                  value={manualAmount}
                  onChange={(e) => setManualAmount(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleAddExpense()}
                  className="w-full bg-zinc-800 text-zinc-200 text-sm rounded-lg pl-7 pr-3 py-2 border border-zinc-700 focus:border-brand-500 focus:outline-none"
                  min="0"
                  step="0.01"
                  disabled={submitting}
                />
              </div>
              <button
                onClick={handleAddExpense}
                disabled={submitting}
                className="px-4 py-2 bg-brand-600 hover:bg-brand-700 disabled:opacity-50 text-white text-sm font-medium rounded-lg transition-colors"
              >
                {submitting ? <Loader2 size={16} className="animate-spin" /> : 'Add'}
              </button>
            </div>
          </div>

          {/* Recent transactions */}
          {transactions.length > 0 && (
            <div className="mt-4 space-y-1.5">
              {transactions.slice(0, 5).map((t) => (
                <div key={t.id} className="flex justify-between text-sm">
                  <span className="text-zinc-400 truncate">{t.name}</span>
                  <span className="text-zinc-300 font-mono">{formatCurrency(t.amount)}</span>
                </div>
              ))}
              {transactions.length > 5 && (
                <p className="text-xs text-zinc-500">+{transactions.length - 5} more</p>
              )}
            </div>
          )}
        </div>

        {/* Retirement Meter */}
        <RetirementMeter config={config} />
      </div>

      {/* Monthly Plan Summary */}
      <div className="glass p-5">
        <h3 className="text-sm font-semibold text-zinc-300 uppercase tracking-wider mb-3 flex items-center gap-2">
          <Calendar size={16} />
          Monthly Plan
        </h3>
        <div className="grid grid-cols-3 gap-4 text-center">
          <div>
            <p className="text-lg font-bold text-emerald-400">{formatCurrency(config.monthly_discretionary)}</p>
            <p className="text-xs text-zinc-500">Guilt-Free</p>
          </div>
          <div>
            <p className="text-lg font-bold text-brand-500">{formatCurrency(config.monthly_investing)}</p>
            <p className="text-xs text-zinc-500">Investing</p>
          </div>
          <div>
            <p className="text-lg font-bold text-zinc-400">{formatCurrency(config.monthly_buffer)}</p>
            <p className="text-xs text-zinc-500">Buffer</p>
          </div>
        </div>
      </div>

      {/* XP Legend */}
      <div className="glass p-5">
        <h3 className="text-sm font-semibold text-zinc-300 uppercase tracking-wider mb-3">
          How to Earn XP
        </h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
          <div className="flex justify-between text-zinc-400">
            <span>Stay under budget</span>
            <span className="text-brand-500 font-mono">+{XP_AWARDS.budget_under}</span>
          </div>
          <div className="flex justify-between text-zinc-400">
            <span>Monthly investment</span>
            <span className="text-brand-500 font-mono">+{XP_AWARDS.investment_transfer}</span>
          </div>
          <div className="flex justify-between text-zinc-400">
            <span>Windfall split</span>
            <span className="text-brand-500 font-mono">+{XP_AWARDS.boss_defeated}</span>
          </div>
          <div className="flex justify-between text-zinc-400">
            <span>Side quest complete</span>
            <span className="text-brand-500 font-mono">+{XP_AWARDS.side_quest_complete}</span>
          </div>
          <div className="flex justify-between text-zinc-400">
            <span>Quarterly review</span>
            <span className="text-brand-500 font-mono">+{XP_AWARDS.quarterly_review}</span>
          </div>
          <div className="flex justify-between text-zinc-400">
            <span>3-month streak bonus</span>
            <span className="text-brand-500 font-mono">+{XP_AWARDS.streak_milestone}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
