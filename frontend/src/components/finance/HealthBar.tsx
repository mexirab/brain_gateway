'use client';

import { getHealthBarStatus, healthBarColor, formatCurrency } from '@/lib/finance-utils';
import type { HealthBarStatus } from '@/lib/finance-types';

interface HealthBarProps {
  spent: number;
  budget: number;
  sideQuestCarve?: number;
}

export default function HealthBar({ spent, budget, sideQuestCarve = 0 }: HealthBarProps) {
  const effectiveBudget = budget - sideQuestCarve;
  const status = getHealthBarStatus(spent, effectiveBudget);
  const percentSpent = effectiveBudget > 0 ? Math.min((spent / effectiveBudget) * 100, 110) : 100;
  const remaining = effectiveBudget - spent;
  const barColor = healthBarColor(status);

  return (
    <div className="glass p-5">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-zinc-300 uppercase tracking-wider">
          Discretionary Budget
        </h3>
        <StatusPill status={status} />
      </div>

      {/* Bar container */}
      <div className="relative h-6 bg-zinc-800 rounded-full overflow-hidden">
        {/* Side quest carve-out indicator */}
        {sideQuestCarve > 0 && (
          <div
            className="absolute right-0 top-0 h-full bg-brand-700/30 border-l border-brand-500/40"
            style={{ width: `${(sideQuestCarve / budget) * 100}%` }}
          />
        )}

        {/* Spent bar */}
        <div
          className={`h-full rounded-full transition-all duration-700 ease-out ${barColor}`}
          style={{ width: `${Math.min(percentSpent, 100)}%` }}
        />
      </div>

      {/* Labels */}
      <div className="flex justify-between mt-2 text-sm">
        <span className="text-zinc-400">
          {formatCurrency(spent)} spent
        </span>
        <span className={remaining >= 0 ? 'text-zinc-300' : 'text-red-400 font-semibold'}>
          {remaining >= 0
            ? `${formatCurrency(remaining)} remaining`
            : `${formatCurrency(Math.abs(remaining))} over budget`}
        </span>
      </div>

      {sideQuestCarve > 0 && (
        <p className="text-xs text-brand-500 mt-1">
          {formatCurrency(sideQuestCarve)} reserved for side quests
        </p>
      )}
    </div>
  );
}

function StatusPill({ status }: { status: HealthBarStatus }) {
  const config: Record<HealthBarStatus, { label: string; classes: string }> = {
    safe: { label: 'On Track', classes: 'bg-emerald-500/20 text-emerald-400' },
    caution: { label: 'Watch It', classes: 'bg-yellow-500/20 text-yellow-400' },
    warning: { label: 'Careful', classes: 'bg-orange-500/20 text-orange-400' },
    danger: { label: 'Critical', classes: 'bg-red-500/20 text-red-400' },
    over: { label: 'Over Budget', classes: 'bg-red-700/30 text-red-300 animate-pulse' },
  };
  const { label, classes } = config[status];
  return (
    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${classes}`}>
      {label}
    </span>
  );
}
