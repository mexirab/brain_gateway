'use client';

import { xpProgress } from '@/lib/finance-utils';

interface XPBarProps {
  totalXP: number;
  level: number;
}

export default function XPBar({ totalXP, level }: XPBarProps) {
  const { current, needed, percent } = xpProgress(totalXP, level);

  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-xs text-content-secondary font-medium">
          XP to Level {level + 1}
        </span>
        <span className="text-xs text-content-muted">
          {current} / {needed}
        </span>
      </div>
      <div className="h-2.5 bg-surface-raised rounded-full overflow-hidden">
        <div
          className="h-full bg-gradient-to-r from-brand to-accent-violet rounded-full transition-all duration-1000 ease-out"
          style={{ width: `${percent}%` }}
        />
      </div>
    </div>
  );
}
