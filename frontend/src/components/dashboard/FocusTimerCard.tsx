'use client';

import { useEffect, useState, useCallback } from 'react';
import { Timer, Play, Square } from 'lucide-react';
import { Card, Button } from '@/components/ui';
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
    <Card>
      <h2 className="text-lg font-semibold text-content-primary mb-3 flex items-center gap-2">
        <Timer size={18} className="text-success" />
        Focus Timer
      </h2>

      {loading && <div className="h-20 bg-surface-raised/50 rounded-lg animate-pulse" />}
      {error && <p className="text-sm text-danger/70">{error}</p>}

      {!loading && !error && focus?.active && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium text-white">{focus.task}</p>
            <span className="text-lg font-mono text-success">
              {Math.floor(focus.remaining_minutes || 0)}m
            </span>
          </div>
          {/* Progress bar */}
          <div className="h-2 bg-surface-raised rounded-full overflow-hidden">
            <div
              className="h-full bg-success rounded-full transition-all duration-1000"
              style={{ width: `${progressPct}%` }}
            />
          </div>
          <Button
            variant="danger"
            size="sm"
            onClick={handleStop}
            disabled={acting}
            className="gap-2"
          >
            <Square size={14} />
            Stop
          </Button>
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
            className="input w-full"
          />
          <div className="flex items-center gap-3">
            <select
              value={durationInput}
              onChange={(e) => setDurationInput(Number(e.target.value))}
              className="input"
            >
              <option value={15}>15 min</option>
              <option value={25}>25 min</option>
              <option value={45}>45 min</option>
              <option value={60}>60 min</option>
            </select>
            <Button
              variant="primary"
              size="sm"
              onClick={handleStart}
              disabled={acting || !taskInput.trim()}
              className="gap-2"
            >
              <Play size={14} />
              Start
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}
