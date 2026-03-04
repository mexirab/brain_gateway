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
        <span className="text-xs text-zinc-400 font-medium">
          XP to Level {level + 1}
        </span>
        <span className="text-xs text-zinc-500">
          {current} / {needed}
        </span>
      </div>
      <div className="h-2.5 bg-zinc-800 rounded-full overflow-hidden">
        <div
          className="h-full bg-gradient-to-r from-brand-500 to-purple-500 rounded-full transition-all duration-1000 ease-out"
          style={{ width: `${percent}%` }}
        />
      </div>
    </div>
  );
}
