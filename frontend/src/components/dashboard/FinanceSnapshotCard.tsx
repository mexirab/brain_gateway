'use client';

import { useEffect, useState } from 'react';
import { Coins, TrendingUp } from 'lucide-react';
import { financeApi } from '@/lib/finance-api';
import type { BudgetPeriod, GameState } from '@/lib/finance-types';

export default function FinanceSnapshotCard() {
  const [budget, setBudget] = useState<BudgetPeriod | null>(null);
  const [game, setGame] = useState<GameState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([financeApi.getCurrentBudget(), financeApi.getGameState()])
      .then(([b, g]) => {
        setBudget(b);
        setGame(g);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  const healthPct = budget
    ? Math.max(0, Math.min(100, ((budget.discretionary_budget - budget.discretionary_spent) / budget.discretionary_budget) * 100))
    : 0;

  const healthColor =
    healthPct > 50 ? 'bg-emerald-500' :
    healthPct > 25 ? 'bg-amber-500' :
    healthPct > 0  ? 'bg-orange-500' :
    'bg-red-500';

  return (
    <div className="glass p-5">
      <h2 className="text-lg font-semibold text-zinc-300 mb-3 flex items-center gap-2">
        <Coins size={18} className="text-amber-400" />
        Budget
      </h2>

      {loading && <div className="h-20 bg-zinc-800/50 rounded-lg animate-pulse" />}
      {error && <p className="text-sm text-red-400/70">{error}</p>}

      {!loading && !error && budget && game && (
        <div className="space-y-3">
          {/* Health bar */}
          <div>
            <div className="flex justify-between text-xs text-zinc-400 mb-1">
              <span>${(budget.discretionary_budget - budget.discretionary_spent).toFixed(0)} left</span>
              <span>${budget.discretionary_budget.toFixed(0)}</span>
            </div>
            <div className="h-3 bg-zinc-800 rounded-full overflow-hidden">
              <div
                className={`h-full ${healthColor} rounded-full transition-all`}
                style={{ width: `${healthPct}%` }}
              />
            </div>
          </div>

          {/* Level + XP */}
          <div className="flex items-center justify-between pt-1">
            <div className="flex items-center gap-2">
              <TrendingUp size={14} className="text-indigo-400" />
              <span className="text-sm text-white">
                Lv.{game.level}
              </span>
            </div>
            <span className="text-xs text-zinc-500">
              {game.total_xp} XP
            </span>
          </div>

          {/* Streak */}
          {game.streak_months > 0 && (
            <div className="text-xs text-amber-400">
              {game.streak_months} month streak
            </div>
          )}
        </div>
      )}
    </div>
  );
}
