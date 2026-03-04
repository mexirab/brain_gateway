'use client';

import { Flame } from 'lucide-react';

interface StreakCounterProps {
  months: number;
  best: number;
}

export default function StreakCounter({ months, best }: StreakCounterProps) {
  const isActive = months > 0;

  return (
    <div className="flex items-center gap-3">
      <div className={isActive ? 'text-orange-400' : 'text-zinc-600'}>
        <Flame size={28} strokeWidth={1.5} fill={isActive ? 'currentColor' : 'none'} />
      </div>
      <div>
        <p className="text-xs text-zinc-500 uppercase tracking-wider">Streak</p>
        <p className="text-sm font-bold text-zinc-200">
          {months} {months === 1 ? 'month' : 'months'}
        </p>
        {best > months && (
          <p className="text-xs text-zinc-500">Best: {best}</p>
        )}
      </div>
    </div>
  );
}
