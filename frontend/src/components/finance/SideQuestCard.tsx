'use client';

import { useState } from 'react';
import {
  Trophy,
  Gamepad2,
  Monitor,
  Plane,
  Car,
  Gift,
  Gem,
  Target,
  Check,
  Ban,
  Loader2,
} from 'lucide-react';
import { formatCurrency } from '@/lib/finance-utils';
import type { SideQuest } from '@/lib/finance-types';

const ICONS: Record<string, React.ElementType> = {
  trophy: Trophy,
  gamepad: Gamepad2,
  monitor: Monitor,
  plane: Plane,
  car: Car,
  gift: Gift,
  gem: Gem,
  target: Target,
};

interface Props {
  quest: SideQuest;
  onContribute: (questId: number, amount: number) => Promise<void>;
  onComplete: (questId: number) => Promise<void>;
  onAbandon: (questId: number) => Promise<void>;
}

export default function SideQuestCard({
  quest,
  onContribute,
  onComplete,
  onAbandon,
}: Props) {
  const [contributeAmount, setContributeAmount] = useState('');
  const [busy, setBusy] = useState(false);
  const [showActions, setShowActions] = useState(false);

  const Icon = ICONS[quest.icon] || Trophy;
  const progress = quest.target_amount > 0
    ? Math.min(100, (quest.saved_amount / quest.target_amount) * 100)
    : 0;
  const isComplete = quest.status === 'completed';
  const isAbandoned = quest.status === 'abandoned';

  async function handleContribute() {
    const amt = parseFloat(contributeAmount);
    if (isNaN(amt) || amt <= 0) return;
    setBusy(true);
    try {
      await onContribute(quest.id, amt);
      setContributeAmount('');
    } finally {
      setBusy(false);
    }
  }

  async function handleComplete() {
    setBusy(true);
    try {
      await onComplete(quest.id);
    } finally {
      setBusy(false);
    }
  }

  async function handleAbandon() {
    setBusy(true);
    try {
      await onAbandon(quest.id);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className={`glass p-5 transition-all ${
        isComplete
          ? 'border border-amber-500/40 shadow-amber-500/5 shadow-lg'
          : isAbandoned
            ? 'opacity-50 border border-zinc-700'
            : 'border border-zinc-700/50'
      }`}
    >
      {/* Header */}
      <div className="flex items-start gap-3">
        <div
          className={`p-2 rounded-lg ${
            isComplete
              ? 'bg-amber-500/20 text-amber-400'
              : isAbandoned
                ? 'bg-zinc-700/30 text-zinc-500'
                : 'bg-brand-500/10 text-brand-400'
          }`}
        >
          <Icon size={20} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-zinc-200 truncate">
              {quest.name}
            </h3>
            {isComplete && (
              <span className="shrink-0 text-[10px] font-bold text-amber-400 bg-amber-500/15 px-2 py-0.5 rounded-full uppercase tracking-wider">
                Unlocked — Guilt Free
              </span>
            )}
            {isAbandoned && (
              <span className="shrink-0 text-[10px] font-medium text-zinc-500 bg-zinc-700/30 px-2 py-0.5 rounded-full">
                Abandoned
              </span>
            )}
          </div>
          {quest.description && (
            <p className="text-xs text-zinc-500 mt-0.5">{quest.description}</p>
          )}
        </div>
        {!isComplete && !isAbandoned && (
          <button
            onClick={() => setShowActions(!showActions)}
            className="text-zinc-500 hover:text-zinc-300 text-xs transition-colors"
          >
            ···
          </button>
        )}
      </div>

      {/* Progress bar */}
      <div className="mt-3">
        <div className="flex justify-between text-xs mb-1">
          <span className="text-zinc-400">
            {formatCurrency(quest.saved_amount)} / {formatCurrency(quest.target_amount)}
          </span>
          <span className={isComplete ? 'text-amber-400 font-semibold' : 'text-zinc-500'}>
            {Math.round(progress)}%
          </span>
        </div>
        <div className="h-2.5 bg-zinc-800 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-500 ${
              isComplete
                ? 'bg-gradient-to-r from-amber-500 to-yellow-400'
                : 'bg-gradient-to-r from-brand-500 to-purple-500'
            }`}
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>

      {/* Monthly carve info */}
      {quest.monthly_carve > 0 && quest.status === 'active' && (
        <p className="text-[11px] text-zinc-500 mt-1.5">
          Auto-saving {formatCurrency(quest.monthly_carve)}/mo from discretionary
        </p>
      )}

      {/* Contribute input (only for active quests) */}
      {quest.status === 'active' && (
        <div className="mt-3 flex gap-2">
          <div className="relative flex-1">
            <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500 text-xs">$</span>
            <input
              type="number"
              placeholder="0"
              value={contributeAmount}
              onChange={(e) => setContributeAmount(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleContribute()}
              className="w-full bg-zinc-800 text-zinc-200 text-xs rounded-lg pl-6 pr-2 py-1.5 border border-zinc-700 focus:border-brand-500 focus:outline-none"
              min="0"
              step="0.01"
              disabled={busy}
            />
          </div>
          <button
            onClick={handleContribute}
            disabled={busy || !contributeAmount}
            className="px-3 py-1.5 bg-brand-600 hover:bg-brand-700 disabled:opacity-40 text-white text-xs font-medium rounded-lg transition-colors"
          >
            {busy ? <Loader2 size={12} className="animate-spin" /> : 'Contribute'}
          </button>
        </div>
      )}

      {/* Action buttons (complete / abandon) */}
      {showActions && quest.status === 'active' && (
        <div className="mt-3 pt-3 border-t border-zinc-700/50 flex gap-2">
          <button
            onClick={handleComplete}
            disabled={busy}
            className="flex items-center gap-1.5 text-xs text-emerald-400 hover:text-emerald-300 transition-colors"
          >
            <Check size={12} />
            Mark Complete
          </button>
          <button
            onClick={handleAbandon}
            disabled={busy}
            className="flex items-center gap-1.5 text-xs text-zinc-500 hover:text-red-400 transition-colors"
          >
            <Ban size={12} />
            Abandon
          </button>
        </div>
      )}

      {/* Completed date */}
      {isComplete && quest.completed_at && (
        <p className="text-[11px] text-amber-500/70 mt-2">
          Completed {new Date(quest.completed_at).toLocaleDateString('en-US', {
            month: 'short',
            day: 'numeric',
            year: 'numeric',
          })}
        </p>
      )}
    </div>
  );
}
