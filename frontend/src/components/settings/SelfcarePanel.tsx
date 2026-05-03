'use client';

import { useState, useEffect, useCallback } from 'react';
import { Loader2, Plus, X } from 'lucide-react';
import {
  settingsApi,
  type SelfcareCategory,
  type SelfcareSchedule,
} from '@/lib/settings-api';
import { SaveBar } from './IdentityPanel';

const CATEGORY_ORDER = ['meds', 'meals', 'water', 'movement'];

const CATEGORY_LABELS: Record<string, string> = {
  meds: 'Medication',
  meals: 'Meals',
  water: 'Water / Hydration',
  movement: 'Movement / Posture',
};

import type { DirtyRegister } from '@/app/(private)/settings/page';

interface PanelProps {
  registerDirty?: DirtyRegister;
}

export default function SelfcarePanel({ registerDirty }: PanelProps = {}) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [original, setOriginal] = useState<SelfcareSchedule | null>(null);
  const [draft, setDraft] = useState<SelfcareSchedule | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await settingsApi.getSelfcare();
      setOriginal(data);
      setDraft(JSON.parse(JSON.stringify(data)));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load selfcare schedule');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const dirty = draft !== null && original !== null && JSON.stringify(draft) !== JSON.stringify(original);

  useEffect(() => {
    registerDirty?.('selfcare', dirty);
  }, [dirty, registerDirty]);

  function patchCategory(name: string, updater: (cat: SelfcareCategory) => SelfcareCategory) {
    setDraft((d) =>
      d
        ? {
            ...d,
            categories: {
              ...d.categories,
              [name]: updater(d.categories[name] ?? {}),
            },
          }
        : d,
    );
    setStatusMsg(null);
  }

  async function handleSave() {
    if (!draft) return;
    setSaving(true);
    setError(null);
    setStatusMsg(null);
    try {
      const saved = await settingsApi.updateSelfcare(draft);
      setOriginal(saved);
      setDraft(JSON.parse(JSON.stringify(saved)));
      setStatusMsg('Saved.');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  }

  function handleDiscard() {
    if (original) setDraft(JSON.parse(JSON.stringify(original)));
    setStatusMsg(null);
    setError(null);
  }

  if (loading || !draft) {
    return (
      <div className="flex items-center justify-center py-10">
        <Loader2 className="animate-spin text-brand-500" size={24} />
      </div>
    );
  }

  // Order: known categories first, then any extras alphabetically
  const knownNames = CATEGORY_ORDER.filter((n) => n in draft.categories);
  const extras = Object.keys(draft.categories)
    .filter((n) => !CATEGORY_ORDER.includes(n))
    .sort();
  const ordered = [...knownNames, ...extras];

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-white">Selfcare Nudges</h2>
        <p className="text-sm text-zinc-400 mt-1">
          One row per category. Disabled rows fire nothing. Time-based categories (meds) take a list of fixed
          times; the others use an interval.
        </p>
      </div>

      <div className="space-y-3">
        {ordered.map((name) => (
          <CategoryCard
            key={name}
            name={name}
            label={CATEGORY_LABELS[name] ?? name}
            value={draft.categories[name] ?? {}}
            onChange={(updater) => patchCategory(name, updater)}
          />
        ))}
      </div>

      <SaveBar
        dirty={dirty}
        saving={saving}
        statusMsg={statusMsg}
        error={error}
        onSave={handleSave}
        onDiscard={handleDiscard}
      />
    </div>
  );
}

function CategoryCard({
  name,
  label,
  value,
  onChange,
}: {
  name: string;
  label: string;
  value: SelfcareCategory;
  onChange: (updater: (cat: SelfcareCategory) => SelfcareCategory) => void;
}) {
  const isFixedTime = name === 'meds';
  const enabled = value.enabled !== false;

  return (
    <div className={`rounded-lg border p-4 ${enabled ? 'border-zinc-700 bg-zinc-900/40' : 'border-zinc-800 bg-zinc-900/20'}`}>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-white">{label}</h3>
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => onChange((c) => ({ ...c, enabled: e.target.checked }))}
            className="h-4 w-4 accent-brand-500"
          />
          <span className="text-xs text-zinc-400">{enabled ? 'On' : 'Off'}</span>
        </label>
      </div>

      {enabled && (
        <div className="space-y-3">
          {isFixedTime ? (
            <FixedTimesEditor
              times={value.times ?? []}
              onChange={(times) => onChange((c) => ({ ...c, times }))}
            />
          ) : (
            <IntervalEditor
              category={name}
              minutes={value.interval_minutes}
              hours={value.interval_hours}
              onChange={(updates) => onChange((c) => ({ ...c, ...updates }))}
            />
          )}

          {!isFixedTime && (
            <ActiveHoursEditor
              start={value.active_hours?.start ?? '09:00'}
              end={value.active_hours?.end ?? '21:00'}
              onChange={(start, end) =>
                onChange((c) => ({ ...c, active_hours: { start, end } }))
              }
            />
          )}

          <label className="flex flex-col gap-1.5">
            <span className="text-xs uppercase tracking-wider text-zinc-500">
              Message override (optional)
            </span>
            <input
              type="text"
              value={value.message_template ?? ''}
              onChange={(e) => onChange((c) => ({ ...c, message_template: e.target.value }))}
              placeholder="Leave blank for default"
              maxLength={500}
              className="bg-zinc-900 border border-zinc-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
            />
          </label>
        </div>
      )}
    </div>
  );
}

function IntervalEditor({
  category,
  minutes,
  hours,
  onChange,
}: {
  category: string;
  minutes: number | undefined;
  hours: number | undefined;
  onChange: (updates: Partial<SelfcareCategory>) => void;
}) {
  // Pick units by which field is populated, NOT by hardcoded category.
  // Falls back to "meals → hours, others → minutes" only when both are
  // undefined so a stale row written by an older client renders sanely.
  const useHours = hours !== undefined ? true : minutes !== undefined ? false : category === 'meals';
  const value = useHours ? (hours ?? 4) : (minutes ?? 90);

  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-xs uppercase tracking-wider text-zinc-500">
        Interval ({useHours ? 'hours' : 'minutes'})
      </span>
      <input
        type="number"
        min={1}
        max={useHours ? 24 : 24 * 60}
        value={value}
        onChange={(e) => {
          const n = Number(e.target.value);
          if (Number.isNaN(n)) return;
          if (useHours) {
            onChange({ interval_hours: n, interval_minutes: undefined });
          } else {
            onChange({ interval_minutes: n, interval_hours: undefined });
          }
        }}
        className="bg-zinc-900 border border-zinc-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500 w-32"
      />
    </label>
  );
}

function ActiveHoursEditor({
  start,
  end,
  onChange,
}: {
  start: string;
  end: string;
  onChange: (start: string, end: string) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-3">
      <label className="flex flex-col gap-1.5">
        <span className="text-xs uppercase tracking-wider text-zinc-500">Active from</span>
        <input
          type="time"
          value={start}
          onChange={(e) => onChange(e.target.value, end)}
          className="bg-zinc-900 border border-zinc-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
        />
      </label>
      <label className="flex flex-col gap-1.5">
        <span className="text-xs uppercase tracking-wider text-zinc-500">Active until</span>
        <input
          type="time"
          value={end}
          onChange={(e) => onChange(start, e.target.value)}
          className="bg-zinc-900 border border-zinc-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
        />
      </label>
    </div>
  );
}

function FixedTimesEditor({
  times,
  onChange,
}: {
  times: string[];
  onChange: (times: string[]) => void;
}) {
  return (
    <div className="space-y-2">
      <span className="text-xs uppercase tracking-wider text-zinc-500">Fire at these times</span>
      <div className="flex flex-wrap items-center gap-2">
        {times.map((t, i) => (
          <span
            key={i}
            className="flex items-center gap-1 bg-zinc-800 border border-zinc-700 rounded-md pl-2 pr-1 py-1"
          >
            <input
              type="time"
              aria-label="Time"
              value={t}
              onChange={(e) => {
                const next = [...times];
                next[i] = e.target.value;
                onChange(next);
              }}
              className="bg-transparent text-sm text-white focus:outline-none"
            />
            <button
              type="button"
              onClick={() => onChange(times.filter((_, idx) => idx !== i))}
              aria-label={t ? `Remove ${t}` : 'Remove time'}
              className="p-1 text-zinc-500 hover:text-red-400"
            >
              <X size={14} />
            </button>
          </span>
        ))}
        <button
          type="button"
          onClick={() => onChange([...times, '08:00'])}
          className="flex items-center gap-1 px-2 py-1 rounded-md border border-zinc-700 text-zinc-300 text-sm hover:border-zinc-500"
        >
          <Plus size={14} />
          Add time
        </button>
      </div>
    </div>
  );
}
