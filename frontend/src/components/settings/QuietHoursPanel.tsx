'use client';

import { useState, useEffect, useCallback } from 'react';
import { Loader2 } from 'lucide-react';
import { settingsApi, type QuietHours, type Weekday } from '@/lib/settings-api';
import { SaveBar } from './IdentityPanel';

const DAY_LABELS: Array<{ key: Weekday; label: string }> = [
  { key: 'mon', label: 'Mon' },
  { key: 'tue', label: 'Tue' },
  { key: 'wed', label: 'Wed' },
  { key: 'thu', label: 'Thu' },
  { key: 'fri', label: 'Fri' },
  { key: 'sat', label: 'Sat' },
  { key: 'sun', label: 'Sun' },
];

function normalize(data: Partial<QuietHours>): QuietHours {
  return {
    start: data.start ?? '22:00',
    end: data.end ?? '07:00',
    // `??` not `||` — the user is allowed to save an empty days list
    // (effectively disabling quiet hours), and we shouldn't reset to all-7.
    days: (data.days as Weekday[]) ?? ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
  };
}

function formatHumanWindow(start: string, end: string, days: Weekday[]): string {
  if (!days.length) return 'No days selected — quiet hours never apply.';
  const dayPart =
    days.length === 7
      ? 'every day'
      : days
          .map((d) => DAY_LABELS.find((x) => x.key === d)?.label ?? d)
          .join(', ');
  return `Jess will be quiet ${start}–${end} on ${dayPart}.`;
}

import type { DirtyRegister } from '@/app/(private)/settings/page';
import { friendlyError } from '@/lib/errors';

interface PanelProps {
  registerDirty?: DirtyRegister;
}

export default function QuietHoursPanel({ registerDirty }: PanelProps = {}) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [original, setOriginal] = useState<QuietHours | null>(null);
  const [draft, setDraft] = useState<QuietHours | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await settingsApi.getQuietHours();
      const norm = normalize(data);
      setOriginal(norm);
      setDraft(norm);
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t load quiet hours.'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const dirty = draft !== null && original !== null && JSON.stringify(draft) !== JSON.stringify(original);

  useEffect(() => {
    registerDirty?.('quiet', dirty);
  }, [dirty, registerDirty]);

  function toggleDay(day: Weekday) {
    setDraft((d) =>
      d
        ? {
            ...d,
            days: d.days.includes(day) ? d.days.filter((x) => x !== day) : [...d.days, day],
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
      const saved = await settingsApi.updateQuietHours(draft);
      const norm = normalize(saved);
      setOriginal(norm);
      setDraft(norm);
      setStatusMsg('Saved.');
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t save your changes.', { preferDetail: true }));
    } finally {
      setSaving(false);
    }
  }

  function handleDiscard() {
    if (original) setDraft(original);
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

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-white">Quiet Hours</h2>
        <p className="text-sm text-content-secondary mt-1">
          During this window, selfcare nudges are suppressed. Reminders still fire (they may be urgent).
        </p>
      </div>

      <div className="grid sm:grid-cols-2 gap-4">
        <label className="flex flex-col gap-1.5">
          <span className="text-xs uppercase tracking-wider text-content-muted">Quiet start</span>
          <input
            type="time"
            value={draft.start}
            onChange={(e) => {
              setDraft((d) => (d ? { ...d, start: e.target.value } : d));
              setStatusMsg(null);
            }}
            className="input"
          />
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-xs uppercase tracking-wider text-content-muted">Quiet end</span>
          <input
            type="time"
            value={draft.end}
            onChange={(e) => {
              setDraft((d) => (d ? { ...d, end: e.target.value } : d));
              setStatusMsg(null);
            }}
            className="input"
          />
        </label>
      </div>

      <fieldset>
        <legend className="text-xs uppercase tracking-wider text-content-muted mb-2">
          Apply on these days
        </legend>
        <div className="flex flex-wrap gap-2">
          {DAY_LABELS.map(({ key, label }) => {
            const active = draft.days.includes(key);
            return (
              <button
                key={key}
                type="button"
                onClick={() => toggleDay(key)}
                aria-pressed={active}
                className={`px-3 py-1.5 rounded-full text-sm border transition-colors ${
                  active
                    ? 'border-brand-500/60 bg-brand-500/15 text-brand-500'
                    : 'border-line text-content-secondary hover:border-line-strong hover:text-white'
                }`}
              >
                {label}
              </button>
            );
          })}
        </div>
      </fieldset>

      <p className="text-sm text-content-secondary italic">
        {formatHumanWindow(draft.start, draft.end, draft.days)}
      </p>

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
