'use client';

import { useCallback, useEffect, useState } from 'react';
import { Dumbbell, Sparkles, Check, Trash2, History } from 'lucide-react';
import { api } from '@/lib/api';
import type {
  WorkoutTodayResponse,
  WorkoutToday,
  WorkoutHistorySession,
  WorkoutSet,
} from '@/lib/types';

type SetInputs = Record<number, { weight: string; reps: string }>;

function isActiveWorkout(w: WorkoutTodayResponse | null): w is WorkoutToday {
  return !!w && w.has_workout === true;
}

export default function WorkoutsPage() {
  const [today, setToday] = useState<WorkoutTodayResponse | null>(null);
  const [history, setHistory] = useState<WorkoutHistorySession[]>([]);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [inputs, setInputs] = useState<SetInputs>({});

  const fetchAll = useCallback(async () => {
    try {
      const [t, h] = await Promise.all([
        api.workoutToday(),
        api.workoutHistory(14),
      ]);
      setToday(t);
      setHistory(h.sessions);
      setError(null);

      if (isActiveWorkout(t)) {
        const next: SetInputs = {};
        for (const ex of t.exercises) {
          for (const s of ex.sets) {
            next[s.id] = {
              weight:
                s.weight_lbs?.toString() ??
                s.target_weight_lbs?.toString() ??
                '',
              reps:
                s.reps?.toString() ?? s.target_reps?.toString() ?? '',
            };
          }
        }
        setInputs(next);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const handleGenerate = async () => {
    setGenerating(true);
    setError(null);
    try {
      const res = await api.generateWorkout();
      if (!res.ok) {
        setError(res.error || 'Failed to generate workout');
      } else {
        await fetchAll();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to generate');
    } finally {
      setGenerating(false);
    }
  };

  const handleLogSet = async (s: WorkoutSet, exerciseName: string) => {
    const vals = inputs[s.id];
    if (!vals) return;
    const weight = parseFloat(vals.weight);
    const reps = parseInt(vals.reps, 10);
    if (isNaN(weight) || isNaN(reps)) {
      setError('Enter a valid weight and reps');
      return;
    }
    try {
      await api.logSet({
        exercise: exerciseName,
        weight_lbs: weight,
        reps,
        set_id: s.id,
        workout_id: s.workout_id,
      });
      await fetchAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to log set');
    }
  };

  const handleDeleteSet = async (id: number) => {
    try {
      await api.deleteWorkoutSet(id);
      await fetchAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed');
    }
  };

  const handleEndWorkout = async () => {
    if (!isActiveWorkout(today)) return;
    try {
      await api.endWorkout(today.workout_id);
      await fetchAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed');
    }
  };

  const updateInput = (
    id: number,
    field: 'weight' | 'reps',
    value: string,
  ) => {
    setInputs((prev) => ({
      ...prev,
      [id]: { ...(prev[id] || { weight: '', reps: '' }), [field]: value },
    }));
  };

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-zinc-200 flex items-center gap-3">
          <Dumbbell size={24} className="text-emerald-400" />
          Workouts
        </h1>
        {isActiveWorkout(today) && (
          <span className="text-sm text-zinc-500">
            {today.completed_sets}/{today.total_sets} sets
          </span>
        )}
      </div>

      {error && (
        <div className="glass p-3 text-sm text-red-400/80 border-red-500/30">
          {error}
        </div>
      )}

      {/* Empty state */}
      {!loading && !isActiveWorkout(today) && (
        <div className="glass p-8 text-center space-y-4">
          <p className="text-zinc-500">No workout today yet.</p>
          <button
            onClick={handleGenerate}
            disabled={generating}
            className="inline-flex items-center gap-2 px-5 py-3 bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 rounded-lg hover:bg-emerald-500/30 transition-colors disabled:opacity-50"
          >
            <Sparkles size={16} />
            {generating ? 'Asking Jess…' : 'Ask Jess for a workout'}
          </button>
        </div>
      )}

      {/* Today's plan */}
      {isActiveWorkout(today) && (
        <div className="glass p-5 space-y-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-xs uppercase tracking-wider text-zinc-500">
                {today.workout_type.replace(/_/g, ' ')}
              </div>
              {today.reasoning && (
                <p className="text-sm text-zinc-400 mt-1">{today.reasoning}</p>
              )}
            </div>
            {!today.ended_at && (
              <button
                onClick={handleEndWorkout}
                className="text-xs text-zinc-500 hover:text-emerald-400 whitespace-nowrap"
              >
                End session
              </button>
            )}
          </div>

          {today.exercises.map((ex) => (
            <div
              key={ex.name}
              className="border border-zinc-800 rounded-lg p-4 space-y-3"
            >
              <div className="flex items-center justify-between">
                <h3 className="font-semibold text-zinc-200">{ex.name}</h3>
                <span className="text-[10px] text-zinc-600 uppercase tracking-wide">
                  {ex.muscle_groups.slice(0, 3).join(' · ')}
                </span>
              </div>
              <div className="space-y-1.5">
                {ex.sets.map((s) => {
                  const vals = inputs[s.id] || { weight: '', reps: '' };
                  return (
                    <div
                      key={s.id}
                      className={`flex items-center gap-2 text-sm ${
                        s.completed ? 'opacity-60' : ''
                      }`}
                    >
                      <span className="w-6 text-xs text-zinc-600">
                        {s.set_number}
                      </span>
                      <input
                        type="number"
                        inputMode="decimal"
                        placeholder="lb"
                        value={vals.weight}
                        onChange={(e) =>
                          updateInput(s.id, 'weight', e.target.value)
                        }
                        className="w-20 bg-transparent border border-zinc-700 rounded px-2 py-1 text-zinc-200 text-sm focus:outline-none focus:border-emerald-500"
                        disabled={s.completed}
                      />
                      <span className="text-zinc-600">×</span>
                      <input
                        type="number"
                        inputMode="numeric"
                        placeholder="reps"
                        value={vals.reps}
                        onChange={(e) =>
                          updateInput(s.id, 'reps', e.target.value)
                        }
                        className="w-16 bg-transparent border border-zinc-700 rounded px-2 py-1 text-zinc-200 text-sm focus:outline-none focus:border-emerald-500"
                        disabled={s.completed}
                      />
                      {s.target_reps && !s.completed && (
                        <span className="text-[10px] text-zinc-600">
                          target {s.target_reps}
                          {s.target_weight_lbs
                            ? ` @ ${s.target_weight_lbs}`
                            : ''}
                        </span>
                      )}
                      <div className="ml-auto flex items-center gap-1">
                        {!s.completed ? (
                          <button
                            onClick={() => handleLogSet(s, ex.name)}
                            className="p-1.5 rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 hover:bg-emerald-500/30"
                            aria-label="log set"
                          >
                            <Check size={14} />
                          </button>
                        ) : (
                          <span className="text-emerald-500/60 p-1.5">
                            <Check size={14} />
                          </span>
                        )}
                        <button
                          onClick={() => handleDeleteSet(s.id)}
                          className="p-1.5 text-zinc-600/60 hover:text-red-400 focus:text-red-400"
                          aria-label="delete set"
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}

          <button
            onClick={handleGenerate}
            disabled={generating}
            className="w-full inline-flex items-center justify-center gap-2 py-2 text-xs text-zinc-500 hover:text-emerald-400 transition-colors"
          >
            <Sparkles size={12} />
            Regenerate today&apos;s workout
          </button>
        </div>
      )}

      {/* History */}
      {history.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-zinc-500">
            <History size={12} />
            Recent sessions
          </div>
          {history.map((h) => (
            <div key={h.id} className="glass p-3 text-sm">
              <div className="flex items-center justify-between">
                <span className="text-zinc-300">
                  {new Date(h.started_at).toLocaleDateString()} ·{' '}
                  <span className="text-zinc-500">
                    {h.workout_type.replace(/_/g, ' ')}
                  </span>
                </span>
                <span className="text-xs text-zinc-600">
                  {h.completed_set_count}/{h.set_count} sets ·{' '}
                  {h.total_volume_lbs.toLocaleString()} lb volume
                </span>
              </div>
              <div className="text-[11px] text-zinc-600 mt-1">
                {h.exercises.slice(0, 5).join(', ')}
                {h.exercises.length > 5 ? '…' : ''}
              </div>
            </div>
          ))}
        </div>
      )}

      {loading && (
        <div className="space-y-2">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-24 bg-zinc-800/30 rounded-lg animate-pulse" />
          ))}
        </div>
      )}
    </div>
  );
}
