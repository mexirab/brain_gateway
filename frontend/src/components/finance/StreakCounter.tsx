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
      <div className={isActive ? 'text-accent-flame' : 'text-content-muted'}>
        <Flame size={28} strokeWidth={1.5} fill={isActive ? 'currentColor' : 'none'} />
      </div>
      <div>
        <p className="text-xs text-content-muted uppercase tracking-wider">Streak</p>
        <p className="text-sm font-bold text-content-primary">
          {months} {months === 1 ? 'month' : 'months'}
        </p>
        {best > months && (
          <p className="text-xs text-content-muted">Best: {best}</p>
        )}
      </div>
    </div>
  );
}
