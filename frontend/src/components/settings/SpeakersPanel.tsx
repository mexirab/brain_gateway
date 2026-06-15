'use client';

import { useCallback, useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import {
  settingsApi,
  type DiscoveredSpeaker,
  type SpeakersPayload,
} from '@/lib/settings-api';
import { SaveBar } from './IdentityPanel';
import type { DirtyRegister } from '@/app/(private)/settings/page';

// Sync this map with `CATEGORIES` in orchestrator/announcement_routes.py.
// Adding a new backend category without a row here still renders (using
// the raw key as label via the fallback below) — but a friendly label +
// hint is what makes the UX coherent.
const CATEGORY_DESCRIPTIONS: Record<string, { label: string; hint: string }> = {
  selfcare: {
    label: 'Selfcare nudges',
    hint: 'Meds, water, meals, movement reminders.',
  },
  reminder: {
    label: 'Reminders',
    hint: 'One-shot user reminders + recurring reminders.',
  },
  calendar: {
    label: 'Calendar alerts',
    hint: 'Event countdowns (60 / 30 / 15 / 5 minute warnings).',
  },
  ambient: {
    label: 'Ambient summaries',
    hint: 'Periodic "what’s left today" updates.',
  },
  progress: {
    label: 'Progress recaps',
    hint: 'Daily and weekly summaries.',
  },
  focus: {
    label: 'Focus session events',
    hint: 'Sprint start / end announcements.',
  },
  briefing: {
    label: 'Morning briefing',
    hint: 'The 7am wake-up summary. Volume floor (MORNING_BRIEFING_MIN_VOLUME) still applies.',
  },
  default: {
    label: 'Default (any other)',
    hint: 'Catch-all for any announcement type not listed above. Optional.',
  },
};

interface PanelProps {
  registerDirty?: DirtyRegister;
}

export default function SpeakersPanel({ registerDirty }: PanelProps = {}) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [original, setOriginal] = useState<SpeakersPayload | null>(null);
  const [draft, setDraft] = useState<SpeakersPayload | null>(null);
  const [discovered, setDiscovered] = useState<DiscoveredSpeaker[]>([]);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [routes, disc] = await Promise.all([
        settingsApi.getSpeakers(),
        settingsApi.discoverSpeakers().catch(() => ({ speakers: [] })),
      ]);
      setOriginal(routes);
      setDraft(JSON.parse(JSON.stringify(routes)));
      // Defensive: if discover returned a 200 with malformed body
      // (`disc.speakers` undefined), fall back to empty list rather than
      // crashing on `.map`.
      setDiscovered(disc?.speakers ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load speaker routes');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const dirty =
    draft !== null && original !== null && JSON.stringify(draft) !== JSON.stringify(original);

  useEffect(() => {
    registerDirty?.('speakers', dirty);
  }, [dirty, registerDirty]);

  function patch(category: string, value: string) {
    setDraft((d) =>
      d ? { ...d, routes: { ...d.routes, [category]: value } } : d,
    );
    setStatusMsg(null);
  }

  async function handleSave() {
    if (!draft) return;
    setSaving(true);
    setError(null);
    setStatusMsg(null);
    try {
      // Send the full routes map so the backend persists every category.
      // Empty strings stay empty in `routes` but the response also carries
      // `effective` showing what would be used at dispatch time — the
      // panel renders that as placeholder text so the user can SEE the
      // fallback rather than wondering if their cleared field "stuck".
      const saved = await settingsApi.updateSpeakers(draft.routes);
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

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-white">Speakers</h2>
        <p className="text-sm text-content-secondary mt-1">
          Pick which speaker each kind of announcement plays on. Use a single Home Assistant{' '}
          <code className="text-xs bg-surface-raised px-1 rounded">media_player.*</code> entity, or
          comma-separate for multi-room. Leave blank to inherit the legacy fallback &mdash; the
          field will show that fallback value as a hint.
        </p>
        {discovered.length === 0 && (
          <p className="text-xs text-warning mt-2">
            Couldn&rsquo;t reach Home Assistant for autocomplete suggestions. You can still type
            entity IDs by hand.
          </p>
        )}
      </div>

      <datalist id="ha-speakers-datalist">
        {discovered.map((s) => (
          <option key={s.entity_id} value={s.entity_id}>
            {/* Only surface state for problem speakers — for the common
                'off'/'idle'/'playing' case it's noise. */}
            {s.state === 'unavailable' ? `${s.friendly_name} (unavailable)` : s.friendly_name}
          </option>
        ))}
      </datalist>

      <ul className="space-y-3">
        {draft.categories.map((cat) => {
          const meta = CATEGORY_DESCRIPTIONS[cat] ?? { label: cat, hint: '' };
          const value = draft.routes[cat] ?? '';
          const effective = draft.effective?.[cat] ?? '';
          // When the user clears the field, show the legacy fallback as
          // placeholder text + explicit hint below — addresses the
          // "I cleared it but the field looks the same" UX trap.
          const showFallbackHint = value === '' && effective !== '';
          // a11y: associate the input with the category-name title via id/htmlFor
          // so screen readers announce the right context.
          const titleId = `speakers-cat-title-${cat}`;
          const inputId = `speakers-cat-input-${cat}`;
          return (
            <li
              key={cat}
              className="rounded-lg border border-line bg-surface-base/40 p-3 sm:grid sm:grid-cols-[200px_1fr] sm:gap-4 sm:items-start"
            >
              <div className="mb-2 sm:mb-0">
                <div id={titleId} className="text-sm font-medium text-white">
                  {meta.label}
                </div>
                <div className="text-xs text-content-muted mt-0.5">{meta.hint}</div>
              </div>
              <div className="flex flex-col gap-1.5">
                <label htmlFor={inputId} className="text-xs uppercase tracking-wider text-content-muted">
                  Speaker(s)
                </label>
                <input
                  id={inputId}
                  type="text"
                  list="ha-speakers-datalist"
                  aria-labelledby={titleId}
                  value={value}
                  onChange={(e) => patch(cat, e.target.value)}
                  placeholder={showFallbackHint ? effective : 'media_player.office_max'}
                  maxLength={500}
                  spellCheck={false}
                  className="input font-mono"
                />
                {showFallbackHint && (
                  <span className="text-xs text-content-muted">
                    Using fallback: <code className="text-content-secondary">{effective}</code>
                  </span>
                )}
              </div>
            </li>
          );
        })}
      </ul>

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
