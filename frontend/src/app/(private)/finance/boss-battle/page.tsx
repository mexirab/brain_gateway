'use client';

import { useState } from 'react';
import { Loader2, Swords, CheckCircle, Calendar } from 'lucide-react';
import BossArena from '@/components/finance/BossArena';
import LevelUpOverlay from '@/components/finance/LevelUpOverlay';
import XPToast from '@/components/finance/XPToast';
import { useFinance } from '@/lib/finance-context';
import { financeApi } from '@/lib/finance-api';
import { WINDFALL_MONTHS } from '@/lib/finance-constants';
import { formatCurrency, currentYearMonth } from '@/lib/finance-utils';
import type { Windfall } from '@/lib/finance-types';
import { Card } from '@/components/ui';

export default function BossBattlePage() {
  const {
    gameState,
    budget,
    loading,
    error,
    refresh,
    awardXP,
    lastXPGain,
    clearXPGain,
  } = useFinance();

  const [levelUpTo, setLevelUpTo] = useState<number | null>(null);

  const yearMonth = currentYearMonth();
  const month = yearMonth.split('-')[1];
  const windfall = WINDFALL_MONTHS[month] as 'bonus' | 'espp' | undefined;
  const bossDefeated = budget?.boss_defeated;

  async function handleDefeatBoss(data: {
    type: 'bonus' | 'espp';
    amount: number;
    invest_percent: number;
  }) {
    const result = await financeApi.createWindfall(data);
    if (result.success) {
      const prevLevel = gameState.level;
      const xpType = data.type === 'bonus' ? 'bonus_split' : 'espp_split';
      await awardXP(xpType, `${data.type.toUpperCase()} windfall: ${formatCurrency(data.amount)}`);

      // Check for level up
      const newState = await financeApi.getGameState();
      if (newState.level > prevLevel) {
        setLevelUpTo(newState.level);
      }

      // TTS announce
      try {
        await financeApi.announce(
          `Boss defeated! You split your ${data.type} windfall of ${formatCurrency(data.amount)}. ` +
          `${formatCurrency(result.invest_amount)} invested, ${formatCurrency(result.spend_amount)} guilt-free spend. Nice work!`
        );
      } catch {
        // TTS not critical
      }

      await refresh();
    }
  }

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
          <p className="text-danger font-semibold">Failed to load boss battle</p>
          <p className="text-sm text-content-muted mt-1">{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Level Up Overlay */}
      {levelUpTo && (
        <LevelUpOverlay level={levelUpTo} onDismiss={() => setLevelUpTo(null)} />
      )}

      {/* XP Toast */}
      {lastXPGain && (
        <XPToast
          amount={lastXPGain.amount}
          description={lastXPGain.description}
          onDismiss={clearXPGain}
        />
      )}

      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-content-primary">Boss Battle</h1>
        <p className="text-sm text-content-muted mt-0.5">
          Windfall months — split your bonus/ESPP wisely
        </p>
      </div>

      {/* Current month status */}
      {windfall && !bossDefeated ? (
        <BossArena
          type={windfall}
          defaultInvestPercent={windfall === 'espp' ? 67 : 50}
          onDefeatBoss={handleDefeatBoss}
        />
      ) : windfall && bossDefeated ? (
        <div className="glass p-8 border border-success/30 text-center">
          <CheckCircle size={40} className="text-success mx-auto mb-3" />
          <h2 className="text-lg font-bold text-success">
            {windfall === 'bonus' ? 'Bonus' : 'ESPP'} Boss Already Defeated!
          </h2>
          <p className="text-sm text-content-secondary mt-1">
            You already split this month&apos;s windfall. Great job!
          </p>
        </div>
      ) : (
        <div className="glass p-8 text-center border border-line/50">
          <Swords size={40} className="text-content-muted mx-auto mb-3" />
          <h2 className="text-lg font-semibold text-content-secondary">No Boss This Month</h2>
          <p className="text-sm text-content-muted mt-1">
            Windfall bosses appear in March, June, October, and December
          </p>
        </div>
      )}

      {/* Windfall schedule */}
      <Card>
        <h3 className="text-sm font-semibold text-content-primary uppercase tracking-wider mb-3 flex items-center gap-2">
          <Calendar size={14} />
          Windfall Schedule
        </h3>
        <div className="grid grid-cols-2 gap-3">
          {[
            { month: 'March', type: 'Bonus', est: '$8,231', code: '03' },
            { month: 'June', type: 'ESPP', est: '$8,237', code: '06' },
            { month: 'October', type: 'Bonus', est: '$8,231', code: '10' },
            { month: 'December', type: 'ESPP', est: '$8,237', code: '12' },
          ].map((w) => {
            const isCurrent = w.code === month;
            return (
              <div
                key={w.code}
                className={`flex items-center justify-between p-3 rounded-lg border ${
                  isCurrent
                    ? 'border-accent-gold/30 bg-accent-gold/5'
                    : 'border-line/50 bg-surface-raised/30'
                }`}
              >
                <div>
                  <p className={`text-sm font-medium ${isCurrent ? 'text-accent-gold' : 'text-content-primary'}`}>
                    {w.month}
                  </p>
                  <p className="text-xs text-content-muted">{w.type}</p>
                </div>
                <p className={`text-sm font-mono ${isCurrent ? 'text-accent-gold' : 'text-content-muted'}`}>
                  ~{w.est}
                </p>
              </div>
            );
          })}
        </div>
      </Card>

      {/* Past windfalls */}
      <WindfallHistory />
    </div>
  );
}

function WindfallHistory() {
  const [windfalls, setWindfalls] = useState<Windfall[]>([]);
  const [loaded, setLoaded] = useState(false);

  async function loadHistory() {
    if (loaded) return;
    try {
      const res = await financeApi.getWindfalls();
      setWindfalls(res.windfalls);
    } catch {
      // not critical
    }
    setLoaded(true);
  }

  if (!loaded) {
    return (
      <button
        onClick={loadHistory}
        className="w-full glass p-3 text-sm text-content-muted hover:text-content-secondary transition-colors text-center"
      >
        Show windfall history
      </button>
    );
  }

  if (windfalls.length === 0) {
    return (
      <Card padding="sm" className="text-center text-sm text-content-muted">
        No windfall history yet
      </Card>
    );
  }

  return (
    <Card>
      <h3 className="text-sm font-semibold text-content-primary uppercase tracking-wider mb-3">
        Windfall History
      </h3>
      <div className="space-y-2">
        {windfalls.map((w) => (
          <div key={w.id} className="flex items-center justify-between text-sm">
            <div className="flex items-center gap-2">
              <span className="text-xs text-content-muted">
                {new Date(w.created_at).toLocaleDateString('en-US', {
                  month: 'short',
                  year: 'numeric',
                })}
              </span>
              <span className="text-content-primary capitalize">{w.type}</span>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-success font-mono text-xs">
                ↑{formatCurrency(w.invest_amount || 0)}
              </span>
              <span className="text-brand font-mono text-xs">
                ↓{formatCurrency(w.spend_amount || 0)}
              </span>
              <span className="text-content-secondary font-mono">{formatCurrency(w.amount)}</span>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}
