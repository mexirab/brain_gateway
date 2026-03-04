'use client';

import { useEffect, useState, useCallback } from 'react';
import { Timer, Play, Square } from 'lucide-react';
import { api } from '@/lib/api';
import type { FocusState } from '@/lib/types';

export default function FocusTimerCard() {
  const [focus, setFocus] = useState<FocusState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [taskInput, setTaskInput] = useState('');
  const [durationInput, setDurationInput] = useState(25);
  const [acting, setActing] = useState(false);

  const fetchFocus = useCallback(() => {
    api
      .focus()
      .then(setFocus)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchFocus();
    const interval = setInterval(fetchFocus, 15000);
    return () => clearInterval(interval);
  }, [fetchFocus]);

  const handleStart = async () => {
    if (!taskInput.trim()) return;
    setActing(true);
    try {
      await api.startFocus(taskInput.trim(), durationInput);
      setTaskInput('');
      fetchFocus();
    } catch {
      // silently fail
    } finally {
      setActing(false);
    }
  };

  const handleStop = async () => {
    setActing(true);
    try {
      await api.stopFocus();
      fetchFocus();
    } catch {
      // silently fail
    } finally {
      setActing(false);
    }
  };

  const progressPct = focus?.active && focus.duration && focus.remaining_minutes != null
    ? Math.max(0, Math.min(100, ((focus.duration - focus.remaining_minutes) / focus.duration) * 100))
    : 0;

  return (
    <div className="glass p-5">
      <h2 className="text-lg font-semibold text-zinc-300 mb-3 flex items-center gap-2">
        <Timer size={18} className="text-emerald-400" />
        Focus Timer
      </h2>

      {loading && <div className="h-20 bg-zinc-800/50 rounded-lg animate-pulse" />}
      {error && <p className="text-sm text-red-400/70">{error}</p>}

      {!loading && !error && focus?.active && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium text-white">{focus.task}</p>
            <span className="text-lg font-mono text-emerald-400">
              {Math.floor(focus.remaining_minutes || 0)}m
            </span>
          </div>
          {/* Progress bar */}
          <div className="h-2 bg-zinc-800 rounded-full overflow-hidden">
            <div
              className="h-full bg-emerald-500 rounded-full transition-all duration-1000"
              style={{ width: `${progressPct}%` }}
            />
          </div>
          <button
            onClick={handleStop}
            disabled={acting}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-red-500/20 text-red-400 hover:bg-red-500/30 text-sm transition-colors disabled:opacity-50"
          >
            <Square size={14} />
            Stop
          </button>
        </div>
      )}

      {!loading && !error && !focus?.active && (
        <div className="space-y-3">
          <input
            type="text"
            placeholder="What are you focusing on?"
            value={taskInput}
            onChange={(e) => setTaskInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleStart()}
            className="w-full px-3 py-2 bg-zinc-800/60 border border-zinc-700/50 rounded-lg text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-indigo-500/50"
          />
          <div className="flex items-center gap-3">
            <select
              value={durationInput}
              onChange={(e) => setDurationInput(Number(e.target.value))}
              className="px-2 py-1.5 bg-zinc-800/60 border border-zinc-700/50 rounded-lg text-sm text-zinc-300 focus:outline-none"
            >
              <option value={15}>15 min</option>
              <option value={25}>25 min</option>
              <option value={45}>45 min</option>
              <option value={60}>60 min</option>
            </select>
            <button
              onClick={handleStart}
              disabled={acting || !taskInput.trim()}
              className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30 text-sm transition-colors disabled:opacity-50"
            >
              <Play size={14} />
              Start
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
