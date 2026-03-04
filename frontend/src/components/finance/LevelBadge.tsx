'use client';

import { Shield } from 'lucide-react';
import { LEVELS } from '@/lib/finance-constants';

interface LevelBadgeProps {
  level: number;
}

const LEVEL_COLORS: Record<number, string> = {
  1: 'text-orange-700',    // Copper
  2: 'text-amber-600',     // Bronze
  3: 'text-zinc-300',      // Silver
  4: 'text-yellow-400',    // Gold
  5: 'text-zinc-100',      // Platinum
  6: 'text-cyan-300',      // Diamond
  7: 'text-emerald-400',   // Emerald
  8: 'text-blue-400',      // Sapphire
  9: 'text-red-400',       // Ruby
  10: 'text-zinc-500',     // Obsidian
  11: 'text-amber-300',    // Millionaire
};

export default function LevelBadge({ level }: LevelBadgeProps) {
  const info = LEVELS.find((l) => l.level === level) ?? LEVELS[0];
  const color = LEVEL_COLORS[level] ?? 'text-brand-500';

  return (
    <div className="flex items-center gap-3">
      <div className={`${color}`}>
        <Shield size={32} strokeWidth={1.5} />
      </div>
      <div>
        <p className="text-xs text-zinc-500 uppercase tracking-wider">Level {info.level}</p>
        <p className={`text-sm font-bold ${color}`}>{info.title}</p>
      </div>
    </div>
  );
}
