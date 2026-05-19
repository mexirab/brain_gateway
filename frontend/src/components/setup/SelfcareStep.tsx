'use client';

import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import {
  settingsApi,
  type SelfcareCategory,
  type SelfcareSchedule,
} from '@/lib/settings-api';
import {
  CATEGORY_LABELS,
  orderedCategoryNames,
} from '@/lib/selfcare-categories';

// Fixed-vs-interval is decided by category name — matching SelfcarePanel,
// which keys the same decision off `name === 'meds'`. A field-presence
// heuristic could disagree with what Settings shows for the same data.
function cadenceLabel(name: string, cat: SelfcareCategory): string {
  if (name === 'meds') {
    return cat.times && cat.times.length > 0
      ? `at ${cat.times.join(', ')}`
      : 'no times set';
  }
  if (cat.interval_hours !== undefined) return `every ${cat.interval_hours} h`;
  if (cat.interval_minutes !== undefined) return `every ${cat.interval_minutes} min`;
  return 'default cadence';
}

interface SelfcareStepProps {
  onNext: () => void;
  onBack: () => void;
}

export default function SelfcareStep({ onNext, onBack }: SelfcareStepProps) {
  const [draft, setDraft] = useState<SelfcareSchedule | null>(null);
  const [original, setOriginal] = useState<SelfcareSchedule | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    settingsApi
      .getSelfcare()
      .then((s) => {
        if (cancelled) return;
        setDraft(s);
        setOriginal(s);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Load failed');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function toggle(name: string, enabled: boolean) {
    setDraft((d) =>
      d
        ? {
            ...d,
            categories: {
              ...d.categories,
              [name]: { ...(d.categories[name] ?? {}), enabled },
            },
          }
        : d,
    );
  }

  async function handleNext() {
    setError('');
    if (!draft) {
      onNext(); // load failed — selfcare is optional and editable later
      return;
    }
    const dirty = JSON.stringify(draft) !== JSON.stringify(original);
    if (!dirty) {
      onNext();
      return;
    }
    setSaving(true);
    try {
      await settingsApi.updateSelfcare(draft);
      onNext();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save selfcare settings');
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-8 text-sm text-zinc-500">
        <Loader2 size={16} className="animate-spin" />
        Loading…
      </div>
    );
  }

  const cats = draft?.categories ?? {};
  const names = orderedCategoryNames(cats);

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h2 className="text-xl font-semibold text-white">Selfcare nudges</h2>
        <p className="text-sm text-zinc-400">
          Jess can send gentle nudges for these. Turn off anything you don’t
          want — you can fine-tune the timing later in Settings.
        </p>
      </div>

      {names.length === 0 ? (
        <p className="rounded-lg border border-zinc-800 p-4 text-sm text-zinc-500">
          {error
            ? 'Could not load selfcare categories — you can configure them later in Settings.'
            : 'No selfcare categories configured.'}
        </p>
      ) : (
        <div className="space-y-2">
          {names.map((name) => {
            const cat = cats[name] ?? {};
            const enabled = cat.enabled !== false;
            const label = CATEGORY_LABELS[name] ?? name;
            return (
              <label
                key={name}
                className={`flex cursor-pointer items-center justify-between rounded-lg border p-3 ${
                  enabled ? 'border-zinc-700' : 'border-zinc-800'
                }`}
              >
                <div>
                  <p
                    className={`text-sm font-medium ${enabled ? 'text-white' : 'text-zinc-500'}`}
                  >
                    {label}
                  </p>
                  <p className="text-xs text-zinc-500">
                    {cadenceLabel(name, cat)}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-zinc-400">
                    {enabled ? 'On' : 'Off'}
                  </span>
                  <input
                    type="checkbox"
                    checked={enabled}
                    onChange={(e) => toggle(name, e.target.checked)}
                    className="h-4 w-4 accent-brand-600"
                    aria-label={`${label} nudges`}
                  />
                </div>
              </label>
            );
          })}
        </div>
      )}

      {error && names.length > 0 && (
        <p className="text-sm text-red-400">{error}</p>
      )}

      <div className="flex justify-between">
        <button
          onClick={onBack}
          disabled={saving}
          className="rounded-md border border-zinc-700 px-4 py-2 text-sm text-zinc-300 transition-colors hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-40"
        >
          Back
        </button>
        <button
          onClick={handleNext}
          disabled={saving}
          className="inline-flex items-center gap-2 rounded-lg bg-brand-600 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-brand-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-50"
        >
          {saving && <Loader2 size={14} className="animate-spin" />}
          {saving ? 'Saving…' : 'Continue'}
        </button>
      </div>
    </div>
  );
}
