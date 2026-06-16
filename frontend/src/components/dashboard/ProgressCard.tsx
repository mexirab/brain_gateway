'use client';

import { CheckCircle, Zap, Flame, TrendingUp, TrendingDown, Minus, Brain } from 'lucide-react';
import { Card, ErrorState } from '@/components/ui';
import { useProgress } from '@/lib/hooks';

export default function ProgressCard() {
  const { data, error, isLoading, mutate } = useProgress();
  const today = data?.today ?? null;
  const week = data?.week ?? null;
  const streaks = data?.streaks ?? null;

  const TrendIcon = week?.trend === 'up' ? TrendingUp : week?.trend === 'down' ? TrendingDown : Minus;
  const trendColor = week?.trend === 'up' ? 'text-success' : week?.trend === 'down' ? 'text-danger' : 'text-content-muted';

  // Mini bar chart: normalize heights to max value across 7 days
  const maxActivity = week
    ? Math.max(1, ...week.days.map((d) => d.tasks_completed + d.focus_sessions))
    : 1;

  const activeStreaks = streaks?.streaks.filter((s) => s.current > 0) ?? [];

  return (
    <Card>
      <h2 className="text-lg font-semibold text-content-primary mb-3 flex items-center gap-2">
        <Zap size={18} className="text-warning" />
        Progress
        {week && (
          <TrendIcon size={16} className={`ml-auto ${trendColor}`} />
        )}
      </h2>

      {isLoading && (
        <div className="space-y-3">
          <div className="h-8 bg-surface-raised/50 rounded-lg animate-pulse" />
          <div className="h-16 bg-surface-raised/50 rounded-lg animate-pulse" />
          <div className="h-6 bg-surface-raised/50 rounded-lg animate-pulse" />
        </div>
      )}
      {!isLoading && error && (
        <ErrorState compact message="Couldn’t load progress." onRetry={() => mutate()} />
      )}

      {!isLoading && !error && today && (
        <div className="space-y-4">
          {/* Today's stats */}
          <div className="grid grid-cols-3 gap-3">
            <div className="text-center">
              <div className="flex items-center justify-center gap-1">
                <CheckCircle size={14} className="text-success" />
                <span className="text-xl font-bold text-white">{today.tasks_completed}</span>
              </div>
              <p className="text-xs text-content-muted">tasks</p>
            </div>
            <div className="text-center">
              <div className="flex items-center justify-center gap-1">
                <Zap size={14} className="text-warning" />
                <span className="text-xl font-bold text-white">{today.focus_minutes}</span>
              </div>
              <p className="text-xs text-content-muted">focus min</p>
            </div>
            <div className="text-center">
              <div className="flex items-center justify-center gap-1">
                <Brain size={14} className="text-brand" />
                <span className="text-xl font-bold text-white">{today.brain_dumps}</span>
              </div>
              <p className="text-xs text-content-muted">dumps</p>
            </div>
          </div>

          {/* 7-day bar chart */}
          {week && (
            <div className="flex items-end gap-1 h-16">
              {week.days.map((d) => {
                const activity = d.tasks_completed + d.focus_sessions;
                const heightPct = Math.max(4, (activity / maxActivity) * 100);
                const isToday = d.date === today.date;
                return (
                  <div key={d.date} className="flex-1 flex flex-col items-center gap-1">
                    <div
                      className={`w-full rounded-sm transition-all ${
                        isToday ? 'bg-success' : 'bg-surface-overlay'
                      }`}
                      style={{ height: `${heightPct}%` }}
                      title={`${d.date}: ${d.tasks_completed} tasks, ${d.focus_sessions} sessions`}
                    />
                    <span className="text-[10px] text-content-muted">
                      {new Date(d.date + 'T00:00:00').toLocaleDateString('en', { weekday: 'narrow' })}
                    </span>
                  </div>
                );
              })}
            </div>
          )}

          {/* Streaks */}
          {activeStreaks.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {activeStreaks.map((s) => (
                <span
                  key={s.category}
                  className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-warning/10 text-warning text-xs"
                >
                  <Flame size={12} />
                  {s.category.replace('_', ' ')} {s.current}d
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
