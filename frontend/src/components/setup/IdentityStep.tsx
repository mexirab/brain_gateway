'use client';

import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { settingsApi, type Identity, type ToneOption } from '@/lib/settings-api';

const TONE_OPTIONS: { value: ToneOption; label: string }[] = [
  { value: '', label: 'Default' },
  { value: 'warm', label: 'Warm' },
  { value: 'balanced', label: 'Balanced' },
  { value: 'direct', label: 'Direct' },
];

const INPUT =
  'w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-white focus:border-brand-500 focus:outline-none';
const LABEL = 'mb-1.5 block text-xs uppercase tracking-wider text-zinc-500';

interface IdentityStepProps {
  /** Called with the saved identity so the shell can hand it to the Review step. */
  onNext: (identity: Identity) => void;
  onBack: () => void;
}

export default function IdentityStep({ onNext, onBack }: IdentityStepProps) {
  const [draft, setDraft] = useState<Identity | null>(null);
  const [original, setOriginal] = useState<Identity | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    settingsApi
      .getIdentity()
      .then((id) => {
        if (cancelled) return;
        setDraft(id);
        setOriginal(id);
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

  async function handleNext() {
    if (!draft) return;
    setError('');
    const dirty = JSON.stringify(draft) !== JSON.stringify(original);
    if (!dirty) {
      onNext(draft);
      return;
    }
    setSaving(true);
    try {
      const saved = await settingsApi.updateIdentity(draft);
      onNext(saved);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save identity settings');
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

  if (!draft) {
    return (
      <div className="space-y-6">
        <p className="text-sm text-red-400">
          {error || 'Could not load identity settings.'}
        </p>
        <div className="flex justify-start">
          <button
            onClick={onBack}
            className="rounded-md border border-zinc-700 px-4 py-2 text-sm text-zinc-300 transition-colors hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
          >
            Back
          </button>
        </div>
      </div>
    );
  }

  const set = (patch: Partial<Identity>) => setDraft({ ...draft, ...patch });

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h2 className="text-xl font-semibold text-white">Identity &amp; tone</h2>
        <p className="text-sm text-zinc-400">
          Tell Jess who she is and who she’s helping.
        </p>
      </div>

      <div className="space-y-4">
        <div>
          <label className={LABEL} htmlFor="setup-assistant-name">
            Assistant name
          </label>
          <input
            id="setup-assistant-name"
            className={INPUT}
            value={draft.assistant_name}
            onChange={(e) => set({ assistant_name: e.target.value })}
            placeholder="Jess"
          />
        </div>

        <div>
          <label className={LABEL} htmlFor="setup-user-name">
            Your name
          </label>
          <input
            id="setup-user-name"
            className={INPUT}
            value={draft.user_name}
            onChange={(e) => set({ user_name: e.target.value })}
            placeholder="Your name"
          />
        </div>

        <div>
          <label className={LABEL} htmlFor="setup-timezone">
            Timezone
          </label>
          <input
            id="setup-timezone"
            className={INPUT}
            value={draft.timezone}
            onChange={(e) => set({ timezone: e.target.value })}
            placeholder="America/Chicago"
          />
        </div>

        <label className="flex items-center gap-3 text-sm text-zinc-300">
          <input
            type="checkbox"
            checked={draft.adhd_mode}
            onChange={(e) => set({ adhd_mode: e.target.checked })}
            className="h-4 w-4 accent-brand-600"
          />
          ADHD mode — tailors tone and pacing for ADHD support
        </label>

        <div>
          <label className={LABEL} htmlFor="setup-tone">
            Tone preference
          </label>
          <select
            id="setup-tone"
            className={INPUT}
            value={draft.tone_preference}
            onChange={(e) => set({ tone_preference: e.target.value as ToneOption })}
          >
            {TONE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

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
