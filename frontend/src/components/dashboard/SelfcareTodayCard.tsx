'use client';

import { useState } from 'react';
import { Heart, Pill, Utensils, Droplet, Activity, Check, ChevronDown, ChevronRight, Circle } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Card, ErrorState } from '@/components/ui';
import { useSelfcareToday } from '@/lib/hooks';
import type { SelfcareAction, SelfcareActionState } from '@/lib/types';

const ACTION_LABEL: Record<SelfcareAction, string> = {
  medication: 'Meds',
  meal: 'Meal',
  water: 'Water',
  movement: 'Movement',
};

const ACTION_ICON: Record<SelfcareAction, LucideIcon> = {
  medication: Pill,
  meal: Utensils,
  water: Droplet,
  movement: Activity,
};

const ORDER: SelfcareAction[] = ['medication', 'meal', 'water', 'movement'];

function formatTimeOnly(iso: string | null): string {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
  });
}

function formatRelative(iso: string | null): string {
  if (!iso) return 'never';
  const then = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - then.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  const diffHr = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHr / 24);

  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  if (diffHr < 24) return `${diffHr}h ago`;
  if (diffDay < 7) return `${diffDay}d ago`;
  const sameYear = then.getFullYear() === now.getFullYear();
  return then.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : { year: 'numeric' }),
  });
}

function ActionRow({ action, state }: { action: SelfcareAction; state: SelfcareActionState }) {
  const [expanded, setExpanded] = useState(false);
  const Icon = ACTION_ICON[action];
  const logged = state.logged_today;
  const hasEntries = state.entries.length > 0;
  const label = ACTION_LABEL[action];
  const ariaLabel = logged
    ? `${label}: ${state.count_today} logged today, last at ${formatTimeOnly(state.last_today)}`
    : state.last_ever
      ? `${label}: not logged today, last ${formatRelative(state.last_ever)}`
      : `${label}: no record ever`;

  return (
    <div className="rounded-lg bg-surface-raised/40 border border-line/30">
      <button
        onClick={() => hasEntries && setExpanded((v) => !v)}
        disabled={!hasEntries}
        aria-expanded={hasEntries ? expanded : undefined}
        aria-disabled={!hasEntries}
        aria-label={ariaLabel}
        className={`w-full flex items-center gap-3 p-2.5 ${
          hasEntries
            ? 'hover:bg-surface-raised/60 cursor-pointer focus-visible:ring-2 focus-visible:ring-accent-violet/50 focus-visible:outline-none'
            : 'cursor-default'
        } rounded-lg transition-colors text-left`}
      >
        <Icon size={16} className={logged ? 'text-success shrink-0' : 'text-content-muted shrink-0'} />
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-2">
            <span className="text-sm font-medium text-white">{label}</span>
            {logged && (
              <span className="text-xs text-success/80">
                {state.count_today}× today
              </span>
            )}
          </div>
          <p className="text-xs text-content-muted truncate">
            {logged && state.last_today ? (
              <>last {formatTimeOnly(state.last_today)}</>
            ) : logged ? (
              <>logged today</>
            ) : state.last_ever ? (
              <>not today — last {formatRelative(state.last_ever)}</>
            ) : (
              <>no record ever</>
            )}
          </p>
        </div>
        {logged ? (
          <Check size={16} className="text-success shrink-0" aria-hidden />
        ) : (
          <Circle size={16} className="text-content-muted shrink-0" aria-hidden />
        )}
        {hasEntries && (
          expanded
            ? <ChevronDown size={14} className="text-content-muted shrink-0" aria-hidden />
            : <ChevronRight size={14} className="text-content-muted shrink-0" aria-hidden />
        )}
      </button>

      {expanded && hasEntries && (
        <div className="border-t border-line/30 px-3 py-2 space-y-1">
          {state.entries.map((e) => (
            <div key={`${e.logged_at}-${e.detail ?? ''}`} className="flex items-baseline justify-between gap-2 text-xs">
              <span className="text-content-secondary truncate">{e.detail || '(no detail)'}</span>
              <span className="text-content-muted shrink-0">{formatTimeOnly(e.logged_at)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function SelfcareTodayCard() {
  const { data, error, isLoading, mutate } = useSelfcareToday();

  return (
    <Card>
      <h2 className="text-lg font-semibold text-content-primary mb-3 flex items-center gap-2">
        <Heart size={18} className="text-accent-violet" />
        Selfcare Today
        {data && (
          <span className="text-xs text-content-muted font-normal">
            {data.today_date}
          </span>
        )}
      </h2>

      {isLoading && (
        <div className="space-y-2">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="h-12 bg-surface-raised/50 rounded-lg animate-pulse" />
          ))}
        </div>
      )}

      {error && !data && (
        <ErrorState compact message="Couldn’t load selfcare state" onRetry={() => mutate()} />
      )}

      {data && (
        <div className="space-y-2">
          {ORDER.map((action) => (
            <ActionRow key={action} action={action} state={data.actions[action]} />
          ))}
          {error && (
            <p className="text-xs text-warning/60">
              (refresh failed — showing last loaded data)
            </p>
          )}
        </div>
      )}
    </Card>
  );
}
