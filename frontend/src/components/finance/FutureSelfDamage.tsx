'use client';

import { TrendingUp, AlertTriangle } from 'lucide-react';
import { formatCurrency, futureSelfDamage } from '@/lib/finance-utils';
import type { FinanceConfig } from '@/lib/finance-types';

interface Props {
  overspend: number;
  config: FinanceConfig;
}

export default function FutureSelfDamage({ overspend, config }: Props) {
  if (overspend <= 0) return null;

  const damage = futureSelfDamage(overspend, config);
  const years = config.retirement_target_age - config.current_age;

  // Severity tiers
  const severe = overspend > 500;
  const critical = overspend > 1000;

  return (
    <div
      className={`glass p-5 border transition-all ${
        critical
          ? 'border-red-500/50 shadow-red-500/10 shadow-lg'
          : severe
            ? 'border-red-500/30'
            : 'border-orange-500/30'
      }`}
    >
      <div className="flex items-start gap-3">
        <div
          className={`mt-0.5 ${
            critical
              ? 'text-red-400 animate-pulse'
              : severe
                ? 'text-red-400'
                : 'text-orange-400'
          }`}
        >
          {critical ? <AlertTriangle size={20} /> : <TrendingUp size={20} />}
        </div>
        <div>
          <h3
            className={`text-sm font-semibold ${
              critical ? 'text-red-400' : severe ? 'text-red-400' : 'text-orange-400'
            }`}
          >
            Future Self Damage
          </h3>
          <p className="text-sm text-zinc-300 mt-1">
            You overspent{' '}
            <span className="text-red-400 font-semibold">
              {formatCurrency(overspend)}
            </span>{' '}
            this month. At 7% over {years} years, that costs Future You{' '}
            <span className="text-red-300 font-bold text-base">
              {formatCurrency(damage)}
            </span>
            .
          </p>
          {critical && (
            <p className="text-xs text-red-400/80 mt-2 italic">
              That&apos;s more than your entire monthly budget wiped out.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
