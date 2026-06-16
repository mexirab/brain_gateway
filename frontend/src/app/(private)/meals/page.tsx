'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { UtensilsCrossed, Plus, Camera, Trash2, X } from 'lucide-react';
import { api } from '@/lib/api';
import { Button, Card, Skeleton } from '@/components/ui';
import { friendlyError } from '@/lib/errors';
import type { Meal, MealsToday, MealHistoryResponse } from '@/lib/types';

type MealType = 'breakfast' | 'lunch' | 'dinner' | 'snack';

export default function MealsPage() {
  const [today, setToday] = useState<MealsToday | null>(null);
  const [history, setHistory] = useState<MealHistoryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [showAdd, setShowAdd] = useState(false);
  const [description, setDescription] = useState('');
  const [calories, setCalories] = useState('');
  const [mealType, setMealType] = useState<MealType>('snack');
  const [saving, setSaving] = useState(false);

  const [uploading, setUploading] = useState(false);
  const [estimateNote, setEstimateNote] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const fetchAll = useCallback(async () => {
    try {
      const [t, h] = await Promise.all([api.mealsToday(), api.mealsHistory(7)]);
      setToday(t);
      setHistory(h);
      setError(null);
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t load your meals.'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const resetForm = () => {
    setDescription('');
    setCalories('');
    setMealType('snack');
    setEstimateNote(null);
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!description.trim()) return;
    setSaving(true);
    try {
      await api.createMeal({
        description: description.trim(),
        calories: calories ? parseInt(calories, 10) : null,
        meal_type: mealType,
      });
      resetForm();
      setShowAdd(false);
      await fetchAll();
    } catch (err) {
      setError(friendlyError(err, 'Couldn’t save that meal.'));
    } finally {
      setSaving(false);
    }
  };

  const handlePhoto = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setEstimateNote(null);
    try {
      const res = await api.uploadMealPhoto(file, false);
      if (!res.ok || !res.estimate) {
        setError(res.error || 'Vision estimate failed');
        return;
      }
      setDescription(res.estimate.description);
      setCalories(res.estimate.calories?.toString() || '');
      setEstimateNote(
        `Estimate confidence: ${res.estimate.confidence}. Edit anything before saving.`,
      );
      setShowAdd(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await api.deleteMeal(id);
      await fetchAll();
    } catch (err) {
      setError(friendlyError(err, 'Couldn’t delete that meal.'));
    }
  };

  const weekTotals = history?.history.slice().reverse() || [];
  const maxCal = Math.max(1, ...weekTotals.map((d) => d.total_calories));

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-content-primary flex items-center gap-3">
          <UtensilsCrossed size={24} className="text-success" />
          Meals
        </h1>
        {today && (
          <span className="text-sm text-content-muted">
            {today.total_calories} kcal · {today.meal_count} meals
          </span>
        )}
      </div>

      {error && (
        <Card padding="none" className="p-3 text-sm text-danger/80 border-danger/30">
          {error}
          <button onClick={() => setError(null)} className="ml-2 text-content-muted">
            <X size={12} className="inline" />
          </button>
        </Card>
      )}

      {/* Add actions */}
      <div className="flex gap-2">
        <Button
          variant="primary"
          onClick={() => {
            resetForm();
            setShowAdd(true);
          }}
          className="flex-1"
        >
          <Plus size={16} />
          Log meal
        </Button>
        <label
          className={`flex-1 flex items-center justify-center gap-2 px-4 py-3 border rounded-lg cursor-pointer transition-colors ${
            uploading
              ? 'bg-surface-raised/30 text-content-muted border-line-subtle cursor-wait'
              : 'bg-info/20 text-info border-info/30 hover:bg-info/30'
          }`}
        >
          <Camera size={16} />
          {uploading ? 'Estimating…' : 'Photo estimate'}
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            capture="environment"
            onChange={handlePhoto}
            className="hidden"
            disabled={uploading}
          />
        </label>
      </div>

      {/* Add form */}
      {showAdd && (
        <form
          onSubmit={handleSave}
          className="glass p-4 space-y-3 border-success/20"
        >
          {estimateNote && (
            <p className="text-xs text-info/80">{estimateNote}</p>
          )}
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What did you eat?"
            className="input w-full"
            autoFocus
          />
          <div className="flex gap-2">
            <input
              type="number"
              value={calories}
              onChange={(e) => setCalories(e.target.value)}
              placeholder="kcal (optional)"
              className="input flex-1"
            />
            <select
              value={mealType}
              onChange={(e) => setMealType(e.target.value as MealType)}
              className="input text-sm"
            >
              <option value="breakfast">Breakfast</option>
              <option value="lunch">Lunch</option>
              <option value="dinner">Dinner</option>
              <option value="snack">Snack</option>
            </select>
          </div>
          <div className="flex gap-2">
            <Button
              type="submit"
              variant="primary"
              disabled={saving || !description.trim()}
              className="flex-1"
            >
              {saving ? 'Saving…' : 'Save'}
            </Button>
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                resetForm();
                setShowAdd(false);
              }}
            >
              Cancel
            </Button>
          </div>
        </form>
      )}

      {/* Today's meals */}
      {!loading && today && (
        <div className="space-y-2">
          <div className="text-xs uppercase tracking-wider text-content-muted">
            Today
          </div>
          {today.meals.length === 0 && (
            <p className="text-sm text-content-muted text-center py-6">
              Nothing logged yet today.
            </p>
          )}
          {today.meals.map((m: Meal) => (
            <Card key={m.id} padding="none" className="p-3 flex items-center gap-3 group">
              <div className="flex-1">
                <div className="text-content-primary">{m.description}</div>
                <div className="text-[11px] text-content-muted mt-0.5">
                  {new Date(m.logged_at).toLocaleTimeString([], {
                    hour: 'numeric',
                    minute: '2-digit',
                  })}{' '}
                  · {m.meal_type}
                  {m.source === 'photo' ? ' · photo' : ''}
                </div>
              </div>
              <div className="text-right">
                <div className="text-success font-mono text-sm">
                  {m.calories ? `${m.calories} kcal` : '—'}
                </div>
              </div>
              <button
                onClick={() => handleDelete(m.id)}
                className="opacity-40 group-hover:opacity-100 focus:opacity-100 text-content-muted hover:text-danger transition-all"
                aria-label="delete meal"
              >
                <Trash2 size={14} />
              </button>
            </Card>
          ))}
        </div>
      )}

      {/* Week chart */}
      {weekTotals.length > 0 && (
        <Card padding="sm" className="space-y-3">
          <div className="text-xs uppercase tracking-wider text-content-muted">
            Last 7 days
            {history?.stats.avg_calories ? (
              <span className="ml-2 text-content-muted">
                avg {history.stats.avg_calories} kcal
              </span>
            ) : null}
          </div>
          <div className="flex items-end gap-2 h-32">
            {weekTotals.map((d) => {
              const pct = Math.round((d.total_calories / maxCal) * 100);
              const label = new Date(d.date).toLocaleDateString([], {
                weekday: 'short',
              });
              return (
                <div
                  key={d.date}
                  className="flex-1 flex flex-col items-center gap-1"
                >
                  <div
                    className="w-full bg-success/40 rounded-t"
                    style={{ height: `${pct}%`, minHeight: '2px' }}
                    title={`${d.total_calories} kcal`}
                  />
                  <span className="text-[10px] text-content-muted">{label}</span>
                  <span className="text-[10px] text-content-muted font-mono">
                    {d.total_calories}
                  </span>
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {loading && (
        <div className="space-y-2">
          {[...Array(3)].map((_, i) => (
            <Skeleton key={i} className="h-14" />
          ))}
        </div>
      )}
    </div>
  );
}
