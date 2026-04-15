'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { UtensilsCrossed, Plus, Camera, Trash2, X } from 'lucide-react';
import { api } from '@/lib/api';
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
      setError(e instanceof Error ? e.message : 'Failed to load');
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
      setError(err instanceof Error ? err.message : 'Failed to save');
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
      setError(err instanceof Error ? err.message : 'Failed to delete');
    }
  };

  const weekTotals = history?.history.slice().reverse() || [];
  const maxCal = Math.max(1, ...weekTotals.map((d) => d.total_calories));

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-zinc-200 flex items-center gap-3">
          <UtensilsCrossed size={24} className="text-emerald-400" />
          Meals
        </h1>
        {today && (
          <span className="text-sm text-zinc-500">
            {today.total_calories} kcal · {today.meal_count} meals
          </span>
        )}
      </div>

      {error && (
        <div className="glass p-3 text-sm text-red-400/80 border-red-500/30">
          {error}
          <button onClick={() => setError(null)} className="ml-2 text-zinc-500">
            <X size={12} className="inline" />
          </button>
        </div>
      )}

      {/* Add actions */}
      <div className="flex gap-2">
        <button
          onClick={() => {
            resetForm();
            setShowAdd(true);
          }}
          className="flex-1 flex items-center justify-center gap-2 px-4 py-3 bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 rounded-lg hover:bg-emerald-500/30 transition-colors"
        >
          <Plus size={16} />
          Log meal
        </button>
        <label
          className={`flex-1 flex items-center justify-center gap-2 px-4 py-3 border rounded-lg cursor-pointer transition-colors ${
            uploading
              ? 'bg-zinc-800/30 text-zinc-600 border-zinc-800 cursor-wait'
              : 'bg-sky-500/20 text-sky-400 border-sky-500/30 hover:bg-sky-500/30'
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
          className="glass p-4 space-y-3 border-emerald-500/20"
        >
          {estimateNote && (
            <p className="text-xs text-sky-400/80">{estimateNote}</p>
          )}
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What did you eat?"
            className="w-full bg-transparent border border-zinc-700 rounded-lg px-3 py-2 text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-emerald-500"
            autoFocus
          />
          <div className="flex gap-2">
            <input
              type="number"
              value={calories}
              onChange={(e) => setCalories(e.target.value)}
              placeholder="kcal (optional)"
              className="flex-1 bg-transparent border border-zinc-700 rounded-lg px-3 py-2 text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-emerald-500"
            />
            <select
              value={mealType}
              onChange={(e) => setMealType(e.target.value as MealType)}
              className="bg-surface-overlay border border-zinc-700 rounded-lg px-3 py-2 text-zinc-300 text-sm focus:outline-none focus:border-emerald-500"
            >
              <option value="breakfast">Breakfast</option>
              <option value="lunch">Lunch</option>
              <option value="dinner">Dinner</option>
              <option value="snack">Snack</option>
            </select>
          </div>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={saving || !description.trim()}
              className="flex-1 px-4 py-2 bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 rounded-lg hover:bg-emerald-500/30 disabled:opacity-30"
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
            <button
              type="button"
              onClick={() => {
                resetForm();
                setShowAdd(false);
              }}
              className="px-4 py-2 text-zinc-500 hover:text-zinc-300"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {/* Today's meals */}
      {!loading && today && (
        <div className="space-y-2">
          <div className="text-xs uppercase tracking-wider text-zinc-500">
            Today
          </div>
          {today.meals.length === 0 && (
            <p className="text-sm text-zinc-600 text-center py-6">
              Nothing logged yet today.
            </p>
          )}
          {today.meals.map((m: Meal) => (
            <div key={m.id} className="glass p-3 flex items-center gap-3 group">
              <div className="flex-1">
                <div className="text-zinc-200">{m.description}</div>
                <div className="text-[11px] text-zinc-600 mt-0.5">
                  {new Date(m.logged_at).toLocaleTimeString([], {
                    hour: 'numeric',
                    minute: '2-digit',
                  })}{' '}
                  · {m.meal_type}
                  {m.source === 'photo' ? ' · photo' : ''}
                </div>
              </div>
              <div className="text-right">
                <div className="text-emerald-400 font-mono text-sm">
                  {m.calories ? `${m.calories} kcal` : '—'}
                </div>
              </div>
              <button
                onClick={() => handleDelete(m.id)}
                className="opacity-40 group-hover:opacity-100 focus:opacity-100 text-zinc-600 hover:text-red-400 transition-all"
                aria-label="delete meal"
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Week chart */}
      {weekTotals.length > 0 && (
        <div className="glass p-4 space-y-3">
          <div className="text-xs uppercase tracking-wider text-zinc-500">
            Last 7 days
            {history?.stats.avg_calories ? (
              <span className="ml-2 text-zinc-600">
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
                    className="w-full bg-emerald-500/40 rounded-t"
                    style={{ height: `${pct}%`, minHeight: '2px' }}
                    title={`${d.total_calories} kcal`}
                  />
                  <span className="text-[10px] text-zinc-600">{label}</span>
                  <span className="text-[10px] text-zinc-500 font-mono">
                    {d.total_calories}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {loading && (
        <div className="space-y-2">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-14 bg-zinc-800/30 rounded-lg animate-pulse" />
          ))}
        </div>
      )}
    </div>
  );
}
