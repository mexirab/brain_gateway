'use client';

import { Shield } from 'lucide-react';
import { LEVELS } from '@/lib/finance-constants';

interface LevelBadgeProps {
  level: number;
}

const LEVEL_COLORS: Record<number, string> = {
  1: 'text-accent-flame',     // Copper
  2: 'text-accent-flame',     // Bronze
  3: 'text-content-secondary',// Silver
  4: 'text-accent-gold',      // Gold
  5: 'text-content-primary',  // Platinum
  6: 'text-accent-cyan',      // Diamond
  7: 'text-success',          // Emerald
  8: 'text-info',             // Sapphire
  9: 'text-danger',           // Ruby
  10: 'text-content-muted',   // Obsidian
  11: 'text-accent-gold',     // Millionaire
};

export default function LevelBadge({ level }: LevelBadgeProps) {
  const info = LEVELS.find((l) => l.level === level) ?? LEVELS[0];
  const color = LEVEL_COLORS[level] ?? 'text-brand';

  return (
    <div className="flex items-center gap-3">
      <div className={`${color}`}>
        <Shield size={32} strokeWidth={1.5} />
      </div>
      <div>
        <p className="text-xs text-content-muted uppercase tracking-wider">Level {info.level}</p>
        <p className={`text-sm font-bold ${color}`}>{info.title}</p>
      </div>
    </div>
  );
}
