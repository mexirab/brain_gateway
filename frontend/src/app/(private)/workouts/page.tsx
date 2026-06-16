'use client';

import { useCallback, useEffect, useState } from 'react';
import { Dumbbell, Sparkles, Check, Trash2, History, Plus, X } from 'lucide-react';
import { api } from '@/lib/api';
import { Button, Card } from '@/components/ui';
import { friendlyError } from '@/lib/errors';
import type {
  WorkoutTodayResponse,
  WorkoutToday,
  WorkoutHistorySession,
  WorkoutSet,
  ExerciseCatalogEntry,
} from '@/lib/types';

type SetInputs = Record<number, { weight: string; reps: string }>;

function isActiveWorkout(w: WorkoutTodayResponse | null): w is WorkoutToday {
  return !!w && w.has_workout === true;
}

export default function WorkoutsPage() {
  const [today, setToday] = useState<WorkoutTodayResponse | null>(null);
  const [history, setHistory] = useState<WorkoutHistorySession[]>([]);
  const [catalog, setCatalog] = useState<ExerciseCatalogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [inputs, setInputs] = useState<SetInputs>({});
  const [addPickerOpen, setAddPickerOpen] = useState(false);
  const [addSelection, setAddSelection] = useState('');

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
      setError(friendlyError(e, 'Couldn’t load your workout.'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  useEffect(() => {
    api
      .workoutExercises()
      .then(setCatalog)
      .catch((e) => {
        console.warn('Exercise catalog load failed', e);
      });
  }, []);

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
      setError(friendlyError(e, 'Couldn’t generate a workout.'));
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
      setError(friendlyError(e, 'Couldn’t log that set.'));
    }
  };

  const handleDeleteSet = async (id: number) => {
    try {
      await api.deleteWorkoutSet(id);
      await fetchAll();
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t delete that set.'));
    }
  };

  const handleEndWorkout = async () => {
    if (!isActiveWorkout(today)) return;
    try {
      await api.endWorkout(today.workout_id);
      await fetchAll();
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t end the workout.'));
    }
  };

  const handleDeleteWorkout = async (workoutId: number, label: string) => {
    if (!confirm(`Delete ${label} and all its sets? This cannot be undone.`)) return;
    try {
      const res = await api.deleteWorkout(workoutId);
      if (!res.ok) {
        setError('Workout could not be deleted (already gone?)');
        await fetchAll();
        return;
      }
      await fetchAll();
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t delete that workout.'));
    }
  };

  const handleRemoveExercise = async (workoutId: number, name: string) => {
    if (!confirm(`Remove ${name} from today's workout? Completed sets are kept.`)) return;
    try {
      const res = await api.modifyWorkout(workoutId, { remove_exercises: [name] });
      if (!res.ok) {
        setError(`Could not remove ${name}`);
        await fetchAll();
        return;
      }
      await fetchAll();
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t remove that exercise.'));
    }
  };

  const handleAddExercise = async () => {
    if (!isActiveWorkout(today) || !addSelection) return;
    try {
      const res = await api.modifyWorkout(today.workout_id, {
        add_exercises: [addSelection],
      });
      if (!res.ok) {
        setError(`Could not add ${addSelection}`);
        return;
      }
      setAddSelection('');
      setAddPickerOpen(false);
      await fetchAll();
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t add that exercise.'));
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
        <h1 className="text-2xl font-bold text-content-primary flex items-center gap-3">
          <Dumbbell size={24} className="text-success" />
          Workouts
        </h1>
        {isActiveWorkout(today) && (
          <span className="text-sm text-content-muted">
            {today.completed_sets}/{today.total_sets} sets
          </span>
        )}
      </div>

      {error && (
        <Card padding="none" className="p-3 text-sm text-danger/80 border-danger/30">
          {error}
        </Card>
      )}

      {/* Empty state */}
      {!loading && !isActiveWorkout(today) && (
        <Card padding="none" className="p-8 text-center space-y-4">
          <p className="text-content-muted">No workout today yet.</p>
          <Button
            variant="primary"
            onClick={handleGenerate}
            disabled={generating}
          >
            <Sparkles size={16} />
            {generating ? 'Asking Jess…' : 'Ask Jess for a workout'}
          </Button>
        </Card>
      )}

      {/* Today's plan */}
      {isActiveWorkout(today) && (
        <Card className="space-y-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-xs uppercase tracking-wider text-content-muted">
                {today.workout_type.replace(/_/g, ' ')}
              </div>
              {today.reasoning && (
                <p className="text-sm text-content-secondary mt-1">{today.reasoning}</p>
              )}
            </div>
            <div className="flex items-center gap-3">
              {!today.ended_at && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={handleEndWorkout}
                  className="whitespace-nowrap"
                >
                  End session
                </Button>
              )}
              <button
                type="button"
                onClick={() =>
                  handleDeleteWorkout(today.workout_id, "today's workout")
                }
                className="p-1.5 text-content-muted hover:text-danger"
                aria-label="Delete today's workout"
                title="Delete today's workout"
              >
                <Trash2 size={14} />
              </button>
            </div>
          </div>

          {today.exercises.map((ex) => (
            <div
              key={ex.name}
              className="border border-line-subtle rounded-lg p-4 space-y-3"
            >
              <div className="flex items-center justify-between gap-2">
                <h3 className="font-semibold text-content-primary">{ex.name}</h3>
                <div className="flex items-center gap-2">
                  <span className="text-[10px] text-content-muted uppercase tracking-wide">
                    {ex.muscle_groups.slice(0, 3).join(' · ')}
                  </span>
                  {!today.ended_at && (
                    <button
                      type="button"
                      onClick={() =>
                        handleRemoveExercise(today.workout_id, ex.name)
                      }
                      className="p-2 text-content-muted/60 hover:text-danger"
                      aria-label={`Remove ${ex.name}`}
                      title="Remove exercise (completed sets are kept)"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
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
                      <span className="w-6 text-xs text-content-muted">
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
                        className="input w-20 text-sm"
                        disabled={s.completed}
                      />
                      <span className="text-content-muted">×</span>
                      <input
                        type="number"
                        inputMode="numeric"
                        placeholder="reps"
                        value={vals.reps}
                        onChange={(e) =>
                          updateInput(s.id, 'reps', e.target.value)
                        }
                        className="input w-16 text-sm"
                        disabled={s.completed}
                      />
                      {s.target_reps && !s.completed && (
                        <span className="text-[10px] text-content-muted">
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
                            className="p-1.5 rounded bg-success/20 text-success border border-success/30 hover:bg-success/30"
                            aria-label="log set"
                          >
                            <Check size={14} />
                          </button>
                        ) : (
                          <span className="text-success/60 p-1.5">
                            <Check size={14} />
                          </span>
                        )}
                        <button
                          onClick={() => handleDeleteSet(s.id)}
                          className="p-1.5 text-content-muted/60 hover:text-danger focus:text-danger"
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

          {!today.ended_at && (
            <div className="pt-1">
              {!addPickerOpen ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setAddPickerOpen(true)}
                  disabled={catalog.length === 0}
                  className="w-full border border-dashed border-line-subtle"
                >
                  <Plus size={12} />
                  {catalog.length === 0 ? 'Exercise catalog unavailable' : 'Add exercise'}
                </Button>
              ) : (
                <div className="flex items-center gap-2">
                  <select
                    value={addSelection}
                    onChange={(e) => setAddSelection(e.target.value)}
                    className="input flex-1 text-sm"
                  >
                    <option value="">Pick an exercise…</option>
                    {catalog.map((ex) => (
                      <option key={ex.name} value={ex.name}>
                        {ex.name} ({ex.primary_muscle})
                      </option>
                    ))}
                  </select>
                  <Button
                    type="button"
                    variant="primary"
                    size="sm"
                    onClick={handleAddExercise}
                    disabled={!addSelection}
                  >
                    Add
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setAddPickerOpen(false);
                      setAddSelection('');
                    }}
                  >
                    Cancel
                  </Button>
                </div>
              )}
            </div>
          )}

          <Button
            variant="ghost"
            size="sm"
            onClick={handleGenerate}
            disabled={generating}
            className="w-full"
          >
            <Sparkles size={12} />
            Regenerate today&apos;s workout
          </Button>
        </Card>
      )}

      {/* History */}
      {history.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-content-muted">
            <History size={12} />
            Recent sessions
          </div>
          {history.map((h) => (
            <Card key={h.id} padding="none" className="p-3 text-sm">
              <div className="flex items-center justify-between gap-2">
                <span className="text-content-primary">
                  {new Date(h.started_at).toLocaleDateString()} ·{' '}
                  <span className="text-content-muted">
                    {h.workout_type.replace(/_/g, ' ')}
                  </span>
                </span>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-content-muted">
                    {h.completed_set_count}/{h.set_count} sets ·{' '}
                    {h.total_volume_lbs.toLocaleString()} lb volume
                  </span>
                  <button
                    type="button"
                    onClick={() =>
                      handleDeleteWorkout(
                        h.id,
                        `${new Date(h.started_at).toLocaleDateString()} ${h.workout_type.replace(/_/g, ' ')}`,
                      )
                    }
                    className="p-2 text-content-muted/60 hover:text-danger"
                    aria-label={`Delete ${new Date(h.started_at).toLocaleDateString()} workout`}
                    title="Delete this workout"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              </div>
              <div className="text-[11px] text-content-muted mt-1">
                {h.exercises.slice(0, 5).join(', ')}
                {h.exercises.length > 5 ? '…' : ''}
              </div>
            </Card>
          ))}
        </div>
      )}

      {loading && (
        <div className="space-y-2">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-24 bg-surface-raised/30 rounded-lg animate-pulse" />
          ))}
        </div>
      )}
    </div>
  );
}
