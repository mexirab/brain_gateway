'use client';

import { useEffect, useState } from 'react';
import { Loader2, Mic } from 'lucide-react';
import { setupApi } from '@/lib/setup-api';

interface VoiceStepProps {
  onNext: () => void;
  onBack: () => void;
}

const INPUT =
  'w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-white focus:border-brand-500 focus:outline-none disabled:opacity-50';
const LABEL = 'mb-1.5 block text-xs uppercase tracking-wider text-zinc-500';
const BTN_PRIMARY =
  'inline-flex items-center gap-2 rounded-lg bg-brand-600 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-brand-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-50';
const BTN_SECONDARY =
  'inline-flex items-center gap-2 rounded-md border border-zinc-700 px-4 py-2 text-sm text-zinc-300 transition-colors hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-40';

export default function VoiceStep({ onNext, onBack }: VoiceStepProps) {
  const [draft, setDraft] = useState('');
  const [original, setOriginal] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setupApi
      .getEnv()
      .then((env) => {
        if (cancelled) return;
        const v = env.keys.TTS_VOICE?.value ?? '';
        setDraft(v);
        setOriginal(v);
      })
      .catch((e) => {
        if (!cancelled) {
          setLoadError(e instanceof Error ? e.message : 'Load failed');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleNext() {
    setError('');
    const v = draft.trim();
    if (v === original) {
      onNext();
      return;
    }
    if (v === '') {
      // Clearing a previously-set value would require DELETE — out of scope.
      // Just advance without writing.
      onNext();
      return;
    }
    setSaving(true);
    try {
      await setupApi.setEnv({ TTS_VOICE: v });
      onNext();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save voice settings');
    } finally {
      setSaving(false);
    }
  }

  function handleSkip() {
    const isDirty = draft.trim() !== original;
    if (
      isDirty &&
      !window.confirm(
        'You have an unsaved voice setting. Skip and discard it?',
      )
    ) {
      return;
    }
    onNext();
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-8 text-sm text-zinc-500">
        <Loader2 size={16} className="animate-spin" />
        Loading…
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h2 className="text-xl font-semibold text-white">Voice</h2>
        <p className="text-sm text-zinc-400">
          Pick the voice Jess speaks with. Leave blank to keep the current
          setting — you can change voices, or set up your own clone, later in
          Settings.
        </p>
      </div>

      {loadError && (
        <div className="rounded-lg border border-red-900/40 bg-red-950/30 p-3 text-sm text-red-300">
          Couldn’t load current voice setting: {loadError}. You can still pick
          one below.
        </div>
      )}

      <div className="flex items-start gap-3 rounded-lg border border-zinc-800 p-4 text-sm">
        <Mic size={16} className="mt-0.5 shrink-0 text-zinc-500" />
        <span className="text-zinc-400">
          The available voices depend on the TTS model you picked in the
          previous step. Qwen3-TTS-CustomVoice answers to a built-in voice id
          (<code className="text-zinc-300">aiden</code>) by default; the{' '}
          <code className="text-zinc-300">*-Base</code> models support voice
          cloning from a reference clip. Additional voices, including clones,
          are loaded into{' '}
          <code className="text-zinc-300">/voices</code> on the TTS server
          after Jess is running.
        </span>
      </div>

      <div>
        <label className={LABEL} htmlFor="setup-tts-voice">
          Voice id
        </label>
        <input
          id="setup-tts-voice"
          className={INPUT}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="default"
          spellCheck={false}
          autoComplete="off"
        />
        <p className="mt-1.5 text-xs text-zinc-500">
          Free-text. Bad values are caught when the TTS server first tries to
          synthesize — there’s no live check here.
        </p>
        {draft.trim() === '' && original !== '' && (
          <p className="mt-1.5 text-xs text-amber-400">
            Clearing this field won’t unset the existing value
            (<code className="text-amber-300">{original}</code>) — only writes
            are supported here. Edit <code>.env</code> or Settings to unset.
          </p>
        )}
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      <div className="flex items-center justify-between">
        <button onClick={onBack} disabled={saving} className={BTN_SECONDARY}>
          Back
        </button>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={handleSkip}
            disabled={saving}
            className="text-xs text-zinc-500 underline-offset-2 hover:text-zinc-300 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-40"
          >
            Skip for now
          </button>
          <button onClick={handleNext} disabled={saving} className={BTN_PRIMARY}>
            {saving && <Loader2 size={14} className="animate-spin" />}
            {saving ? 'Saving…' : 'Continue'}
          </button>
        </div>
      </div>
    </div>
  );
}
