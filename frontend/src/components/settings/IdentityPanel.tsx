'use client';

import { useState, useEffect, useCallback } from 'react';
import { Loader2 } from 'lucide-react';
import { settingsApi, type Identity, type ToneOption } from '@/lib/settings-api';
import { Button } from '@/components/ui';

const TONE_CHOICES: Array<{ value: ToneOption; label: string; hint: string }> = [
  { value: '', label: 'Default', hint: 'Falls back to legacy tone constraint' },
  { value: 'warm', label: 'Warm', hint: 'Empathy first, then redirect' },
  { value: 'balanced', label: 'Balanced', hint: 'Brief warmth, then action' },
  { value: 'direct', label: 'Direct', hint: 'No softening, lead with the answer' },
];

import type { DirtyRegister } from '@/app/(private)/settings/page';
import { friendlyError } from '@/lib/errors';

interface PanelProps {
  registerDirty?: DirtyRegister;
}

export default function IdentityPanel({ registerDirty }: PanelProps = {}) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [original, setOriginal] = useState<Identity | null>(null);
  const [draft, setDraft] = useState<Identity | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await settingsApi.getIdentity();
      setOriginal(data);
      setDraft(data);
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t load your identity settings.'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const dirty = draft !== null && original !== null && JSON.stringify(draft) !== JSON.stringify(original);

  useEffect(() => {
    registerDirty?.('identity', dirty);
  }, [dirty, registerDirty]);

  function patch<K extends keyof Identity>(key: K, value: Identity[K]) {
    setDraft((d) => (d ? { ...d, [key]: value } : d));
    setStatusMsg(null);
  }

  async function handleSave() {
    if (!draft || !original) return;
    setSaving(true);
    setError(null);
    setStatusMsg(null);
    const updates: Partial<Identity> = {};
    (Object.keys(draft) as Array<keyof Identity>).forEach((k) => {
      if (draft[k] !== original[k]) {
        // @ts-expect-error narrowed at runtime
        updates[k] = draft[k];
      }
    });
    try {
      const saved = await settingsApi.updateIdentity(updates);
      setOriginal(saved);
      setDraft(saved);
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
        <h2 className="text-lg font-semibold text-white">Identity & Tone</h2>
        <p className="text-sm text-content-secondary mt-1">
          What Jess calls herself, what she calls you, and how she talks.
        </p>
      </div>

      <div className="grid sm:grid-cols-2 gap-4">
        <label className="flex flex-col gap-1.5">
          <span className="text-xs uppercase tracking-wider text-content-muted">Assistant name</span>
          <input
            type="text"
            value={draft.assistant_name}
            onChange={(e) => patch('assistant_name', e.target.value)}
            maxLength={64}
            className="input"
          />
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-xs uppercase tracking-wider text-content-muted">Your name</span>
          <input
            type="text"
            value={draft.user_name}
            onChange={(e) => patch('user_name', e.target.value)}
            maxLength={64}
            className="input"
          />
        </label>
      </div>

      <label className="flex items-start gap-3 cursor-pointer">
        <input
          type="checkbox"
          checked={draft.adhd_mode}
          onChange={(e) => patch('adhd_mode', e.target.checked)}
          className="mt-1 h-4 w-4 accent-brand-500"
        />
        <span className="flex flex-col">
          <span className="text-sm text-white">ADHD mode</span>
          <span className="text-xs text-content-muted">
            When off, Jess uses a generic tone and skips ADHD-coaching framing.
          </span>
        </span>
      </label>

      <fieldset className="space-y-2">
        <legend className="text-xs uppercase tracking-wider text-content-muted">Tone preference</legend>
        <div className="grid sm:grid-cols-2 gap-2">
          {TONE_CHOICES.map((opt) => (
            <label
              key={opt.value}
              className={`flex items-start gap-2 px-3 py-2 rounded-md border cursor-pointer transition-colors ${
                draft.tone_preference === opt.value
                  ? 'border-brand-500/60 bg-brand-500/10'
                  : 'border-line hover:border-line-strong'
              }`}
            >
              <input
                type="radio"
                name="tone_preference"
                value={opt.value}
                checked={draft.tone_preference === opt.value}
                onChange={() => patch('tone_preference', opt.value)}
                className="mt-1 accent-brand-500"
              />
              <span className="flex flex-col">
                <span className="text-sm text-white">{opt.label}</span>
                <span className="text-xs text-content-muted">{opt.hint}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

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

export function SaveBar({
  dirty,
  saving,
  statusMsg,
  error,
  onSave,
  onDiscard,
}: {
  dirty: boolean;
  saving: boolean;
  statusMsg: string | null;
  error: string | null;
  onSave: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className="flex items-center gap-3 pt-2 border-t border-line-subtle">
      <Button
        type="button"
        variant="primary"
        disabled={!dirty || saving}
        onClick={onSave}
      >
        {saving && <Loader2 size={14} className="animate-spin" />}
        Save changes
      </Button>
      <Button
        type="button"
        variant="secondary"
        disabled={!dirty || saving}
        onClick={onDiscard}
      >
        Discard
      </Button>
      {dirty && !saving && !statusMsg && (
        <span className="text-xs text-warning">Unsaved changes</span>
      )}
      {statusMsg && !error && <span className="text-xs text-success">{statusMsg}</span>}
      {error && <span className="text-xs text-danger">{error}</span>}
    </div>
  );
}
