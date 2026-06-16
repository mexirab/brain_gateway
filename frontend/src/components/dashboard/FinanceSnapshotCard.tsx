'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { Coins, TrendingUp } from 'lucide-react';
import { Card, ErrorState } from '@/components/ui';
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
      .catch(() => setError('Couldn’t load your budget.'))
      .finally(() => setLoading(false));
  }, []);

  const healthPct = budget
    ? Math.max(0, Math.min(100, ((budget.discretionary_budget - budget.discretionary_spent) / budget.discretionary_budget) * 100))
    : 0;

  const healthColor =
    healthPct > 50 ? 'bg-success' :
    healthPct > 25 ? 'bg-warning' :
    healthPct > 0  ? 'bg-warning' :
    'bg-danger';

  return (
    <Card as={Link} href="/finance" className="block hover:border-brand/40 transition-colors cursor-pointer">
      <h2 className="text-lg font-semibold text-content-primary mb-3 flex items-center gap-2">
        <Coins size={18} className="text-warning" />
        Budget
      </h2>

      {loading && <div className="h-20 bg-surface-raised/50 rounded-lg animate-pulse" />}
      {!loading && error && <ErrorState compact message={error} />}

      {!loading && !error && budget && game && (
        <div className="space-y-3">
          {/* Health bar */}
          <div>
            <div className="flex justify-between text-xs text-content-secondary mb-1">
              <span>${(budget.discretionary_budget - budget.discretionary_spent).toFixed(0)} left</span>
              <span>${budget.discretionary_budget.toFixed(0)}</span>
            </div>
            <div className="h-3 bg-surface-raised rounded-full overflow-hidden">
              <div
                className={`h-full ${healthColor} rounded-full transition-all`}
                style={{ width: `${healthPct}%` }}
              />
            </div>
          </div>

          {/* Level + XP */}
          <div className="flex items-center justify-between pt-1">
            <div className="flex items-center gap-2">
              <TrendingUp size={14} className="text-brand" />
              <span className="text-sm text-white">
                Lv.{game.level}
              </span>
            </div>
            <span className="text-xs text-content-muted">
              {game.total_xp} XP
            </span>
          </div>

          {/* Streak */}
          {game.streak_months > 0 && (
            <div className="text-xs text-warning">
              {game.streak_months} month streak
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
