'use client';

import { useCallback, useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { settingsApi, type VoiceStyleMaps, type VoiceStylePayload } from '@/lib/settings-api';
import { SaveBar } from './IdentityPanel';
import type { DirtyRegister } from '@/app/(private)/settings/page';
import { friendlyError } from '@/lib/errors';

// Sync with MODES / ANNOUNCEMENT_TYPES in orchestrator/voice_style.py. A key
// missing here still renders with its raw name.
const MODE_LABELS: Record<string, { label: string; hint: string }> = {
  explainer: { label: 'Everyday replies', hint: 'Questions, requests, small talk — most messages.' },
  mirror: { label: 'Reflecting', hint: 'When you ask her to analyze or reflect on you.' },
  counterbalance: { label: 'Stressed', hint: 'Urgency, all-or-nothing, “everything is falling apart”.' },
  challenge: { label: 'Accountability', hint: 'When you ask to be pushed or held to something.' },
  baseline: { label: 'Overwhelmed', hint: 'High-distress moments.' },
};

const ANNOUNCEMENT_LABELS: Record<string, string> = {
  reminder: 'Reminders',
  calendar: 'Calendar alerts',
  selfcare: 'Selfcare nudges',
  progress: 'Progress recaps & streaks',
  routine: 'Routine steps',
  focus: 'Focus sessions',
  briefing: 'Briefings & wind-down',
  greeting: 'Greetings',
  ambient: 'Ambient summaries',
  interrupt: 'Interruption recovery',
  email: 'Email alerts',
  finance: 'Budget alerts',
  manual: 'Manual announcements',
};

function presetLabel(key: string): string {
  return key === 'auto' ? 'Automatic (from the words)' : key.charAt(0).toUpperCase() + key.slice(1);
}

function untilText(until: number | null): string {
  if (until === null) return 'until you reset it';
  return `until ${new Date(until * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
}

interface PanelProps {
  registerDirty?: DirtyRegister;
}

export default function VoiceStylePanel({ registerDirty }: PanelProps = {}) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [data, setData] = useState<VoiceStylePayload | null>(null);
  const [draft, setDraft] = useState<VoiceStyleMaps | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);
  const [overrideError, setOverrideError] = useState<string | null>(null);

  const apply = useCallback((payload: VoiceStylePayload) => {
    setData(payload);
    setDraft(JSON.parse(JSON.stringify(payload.defaults)));
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      apply(await settingsApi.getVoiceStyle());
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t load voice style settings.'));
    } finally {
      setLoading(false);
    }
  }, [apply]);

  useEffect(() => {
    load();
  }, [load]);

  const dirty =
    draft !== null && data !== null && JSON.stringify(draft) !== JSON.stringify(data.defaults);

  useEffect(() => {
    registerDirty?.('voice', dirty);
  }, [dirty, registerDirty]);

  function patch(group: keyof VoiceStyleMaps, key: string, value: string) {
    setDraft((d) => (d ? { ...d, [group]: { ...d[group], [key]: value } } : d));
    setStatusMsg(null);
  }

  async function handleSave() {
    if (!draft) return;
    setSaving(true);
    setError(null);
    setStatusMsg(null);
    try {
      apply(await settingsApi.updateVoiceStyle(draft));
      setStatusMsg('Saved.');
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t save your changes.', { preferDetail: true }));
    } finally {
      setSaving(false);
    }
  }

  function handleDiscard() {
    if (data) setDraft(JSON.parse(JSON.stringify(data.defaults)));
    setStatusMsg(null);
    setError(null);
  }

  async function handleClearOverride() {
    setClearing(true);
    setOverrideError(null);
    try {
      const payload = await settingsApi.clearVoiceStyleOverride();
      // Keep unsaved edits to the defaults; only refresh the override.
      setData((d) => (d ? { ...d, override: payload.override } : payload));
    } catch (e) {
      setOverrideError(friendlyError(e, 'Couldn’t reset the voice style.'));
    } finally {
      setClearing(false);
    }
  }

  if (loading || !data || !draft) {
    return (
      <div className="flex items-center justify-center py-10">
        {error ? (
          <div className="flex flex-col items-center gap-3">
            <p className="text-sm text-danger">{error}</p>
            <Button variant="secondary" size="sm" onClick={load}>
              Retry
            </Button>
          </div>
        ) : (
          <Loader2 className="animate-spin text-brand-500" size={24} />
        )}
      </div>
    );
  }

  const presetKeys = Object.keys(data.presets);
  // A timed override can lapse while the page is open; the server already ignores it.
  const override =
    data.override && (data.override.until === null || data.override.until * 1000 > Date.now())
      ? data.override
      : null;

  // Plain render helper (not a nested component) so React doesn't remount the selects every render.
  function renderSelect(group: keyof VoiceStyleMaps, item: string, label: string) {
    const id = `voice-${group}-${item}`;
    const value = draft?.[group][item] ?? 'auto';
    const hint = group === 'modes' ? MODE_LABELS[item]?.hint : undefined;
    return (
      <li
        key={id}
        className="rounded-lg border border-line bg-surface-base/40 p-3 sm:grid sm:grid-cols-[220px_1fr] sm:gap-4 sm:items-center"
      >
        <div className="mb-2 sm:mb-0">
          <label htmlFor={id} className="block text-sm font-medium text-white">
            {label}
          </label>
          {hint && (
            <span id={`${id}-hint`} className="block text-xs text-content-muted mt-0.5">
              {hint}
            </span>
          )}
        </div>
        <div className="flex flex-col gap-1">
          <select
            id={id}
            value={value}
            onChange={(e) => patch(group, item, e.target.value)}
            aria-describedby={[hint && `${id}-hint`, value !== 'auto' && `${id}-phrase`].filter(Boolean).join(' ') || undefined}
            className="input"
          >
            {presetKeys.map((k) => (
              <option key={k} value={k}>
                {presetLabel(k)}
              </option>
            ))}
          </select>
          {value !== 'auto' && (
            <span id={`${id}-phrase`} className="text-xs text-content-muted">
              Sounds: “{data!.presets[value]}”
            </span>
          )}
        </div>
      </li>
    );
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-white">Voice Style</h2>
        <p className="text-sm text-content-secondary mt-1">
          How Jess sounds when she speaks. Pick a default for each kind of conversation and each kind
          of announcement. You can also just ask her — “talk softer for an hour”, “be more upbeat”,
          “back to normal” — which overrides everything here until it’s reset.
        </p>
        {!data.enabled && (
          <p className="text-xs text-warning mt-2">
            Voice styling is switched off (TTS_STYLE_ENABLED=false). Changes save but won’t be audible.
          </p>
        )}
      </div>

      {override && (
        <div className="rounded-lg border border-brand-500/40 bg-brand-500/10 p-3 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-white">
              You asked Jess to sound <strong>{presetLabel(override.style).toLowerCase()}</strong>{' '}
              (“{data.presets[override.style]}”) {untilText(override.until)}. It applies to
              everything she says.
            </p>
            <Button type="button" variant="secondary" size="sm" onClick={handleClearOverride} disabled={clearing}>
              {clearing ? 'Resetting…' : 'Reset to defaults'}
            </Button>
          </div>
          {overrideError && <p className="text-xs text-danger">{overrideError}</p>}
        </div>
      )}

      <section className="space-y-2">
        <h3 className="text-sm font-semibold text-content-secondary uppercase tracking-wider">Chat replies</h3>
        <ul className="space-y-3">
          {data.modes.map((m) => (
            renderSelect('modes', m, MODE_LABELS[m]?.label ?? m)
          ))}
        </ul>
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-semibold text-content-secondary uppercase tracking-wider">Announcements</h3>
        <ul className="space-y-3">
          {data.announcement_types.map((t) => (
            renderSelect('announcements', t, ANNOUNCEMENT_LABELS[t] ?? t)
          ))}
        </ul>
      </section>

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
