'use client';

import { useEffect, useState, useCallback } from 'react';
import { CheckCircle, Zap, Flame, TrendingUp, TrendingDown, Minus, Brain } from 'lucide-react';
import { api } from '@/lib/api';
import type { ProgressToday, ProgressWeek, ProgressStreaks } from '@/lib/types';

export default function ProgressCard() {
  const [today, setToday] = useState<ProgressToday | null>(null);
  const [week, setWeek] = useState<ProgressWeek | null>(null);
  const [streaks, setStreaks] = useState<ProgressStreaks | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(() => {
    Promise.all([api.progressToday(), api.progressWeek(), api.progressStreaks()])
      .then(([t, w, s]) => {
        setToday(t);
        setWeek(w);
        setStreaks(s);
        setError(null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 60000);
    return () => clearInterval(interval);
  }, [fetchData]);

  const TrendIcon = week?.trend === 'up' ? TrendingUp : week?.trend === 'down' ? TrendingDown : Minus;
  const trendColor = week?.trend === 'up' ? 'text-emerald-400' : week?.trend === 'down' ? 'text-red-400' : 'text-zinc-500';

  // Mini bar chart: normalize heights to max value across 7 days
  const maxActivity = week
    ? Math.max(1, ...week.days.map((d) => d.tasks_completed + d.focus_sessions))
    : 1;

  const activeStreaks = streaks?.streaks.filter((s) => s.current > 0) ?? [];

  return (
    <div className="glass p-5">
      <h2 className="text-lg font-semibold text-zinc-300 mb-3 flex items-center gap-2">
        <Zap size={18} className="text-amber-400" />
        Progress
        {week && (
          <TrendIcon size={16} className={`ml-auto ${trendColor}`} />
        )}
      </h2>

      {loading && (
        <div className="space-y-3">
          <div className="h-8 bg-zinc-800/50 rounded-lg animate-pulse" />
          <div className="h-16 bg-zinc-800/50 rounded-lg animate-pulse" />
          <div className="h-6 bg-zinc-800/50 rounded-lg animate-pulse" />
        </div>
      )}
      {error && <p className="text-sm text-red-400/70">{error}</p>}

      {!loading && !error && today && (
        <div className="space-y-4">
          {/* Today's stats */}
          <div className="grid grid-cols-3 gap-3">
            <div className="text-center">
              <div className="flex items-center justify-center gap-1">
                <CheckCircle size={14} className="text-emerald-400" />
                <span className="text-xl font-bold text-white">{today.tasks_completed}</span>
              </div>
              <p className="text-xs text-zinc-500">tasks</p>
            </div>
            <div className="text-center">
              <div className="flex items-center justify-center gap-1">
                <Zap size={14} className="text-amber-400" />
                <span className="text-xl font-bold text-white">{today.focus_minutes}</span>
              </div>
              <p className="text-xs text-zinc-500">focus min</p>
            </div>
            <div className="text-center">
              <div className="flex items-center justify-center gap-1">
                <Brain size={14} className="text-violet-400" />
                <span className="text-xl font-bold text-white">{today.brain_dumps}</span>
              </div>
              <p className="text-xs text-zinc-500">dumps</p>
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
                        isToday ? 'bg-emerald-500' : 'bg-zinc-700'
                      }`}
                      style={{ height: `${heightPct}%` }}
                      title={`${d.date}: ${d.tasks_completed} tasks, ${d.focus_sessions} sessions`}
                    />
                    <span className="text-[10px] text-zinc-600">
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
                  className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-orange-500/10 text-orange-400 text-xs"
                >
                  <Flame size={12} />
                  {s.category.replace('_', ' ')} {s.current}d
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
