'use client';

import Link from 'next/link';
import { getHealthBarStatus, healthBarColor, formatCurrency } from '@/lib/finance-utils';
import type { HealthBarStatus } from '@/lib/finance-types';
import { Card } from '@/components/ui';

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
    <Card>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-content-primary uppercase tracking-wider">
          Discretionary Budget
        </h3>
        <StatusPill status={status} />
      </div>

      {/* Bar container */}
      <div className="relative h-6 bg-surface-raised rounded-full overflow-hidden">
        {/* Side quest carve-out indicator */}
        {sideQuestCarve > 0 && (
          <div
            className="absolute right-0 top-0 h-full bg-brand/30 border-l border-brand/40"
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
        <Link
          href="/finance/transactions?filter=discretionary"
          className="text-content-secondary hover:text-brand transition-colors underline decoration-line-strong hover:decoration-brand underline-offset-2"
        >
          {formatCurrency(spent)} spent
        </Link>
        <span className={remaining >= 0 ? 'text-content-primary' : 'text-danger font-semibold'}>
          {remaining >= 0
            ? `${formatCurrency(remaining)} remaining`
            : `${formatCurrency(Math.abs(remaining))} over budget`}
        </span>
      </div>

      {sideQuestCarve > 0 && (
        <p className="text-xs text-brand mt-1">
          {formatCurrency(sideQuestCarve)} reserved for side quests
        </p>
      )}
    </Card>
  );
}

function StatusPill({ status }: { status: HealthBarStatus }) {
  const config: Record<HealthBarStatus, { label: string; classes: string }> = {
    safe: { label: 'On Track', classes: 'bg-success/20 text-success' },
    caution: { label: 'Watch It', classes: 'bg-warning/20 text-warning' },
    warning: { label: 'Careful', classes: 'bg-accent-flame/20 text-accent-flame' },
    danger: { label: 'Critical', classes: 'bg-danger/20 text-danger' },
    over: { label: 'Over Budget', classes: 'bg-danger/30 text-danger animate-pulse' },
  };
  const { label, classes } = config[status];
  return (
    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${classes}`}>
      {label}
    </span>
  );
}
