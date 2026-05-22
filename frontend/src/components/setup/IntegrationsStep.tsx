'use client';

import { useEffect, useState } from 'react';
import {
  Loader2,
  Plug,
  CheckCircle2,
  AlertCircle,
  Eye,
  EyeOff,
} from 'lucide-react';
import { setupApi } from '@/lib/setup-api';

interface IntegrationsStepProps {
  onNext: () => void;
  onBack: () => void;
}

// Integration env keys (matches orchestrator/setup_env.py ALLOWED_KEYS).
// Neither integration has an `*_ENABLED` allow-listed key — they auto-enable
// when their credentials are set. The on/off toggle in the UI is therefore
// purely visual, and gates only whether the fields are editable.
const HA_KEYS = ['HA_URL', 'HA_TOKEN'] as const;
const PAPERLESS_KEYS = ['PAPERLESS_URL', 'PAPERLESS_API_TOKEN'] as const;
const ALL_KEYS = [...HA_KEYS, ...PAPERLESS_KEYS] as const;

interface FormState {
  HA_ENABLED: boolean; // UI-only — not written to env
  HA_URL: string;
  HA_TOKEN: string;
  PAPERLESS_ENABLED: boolean; // UI-only — not written to env
  PAPERLESS_URL: string;
  PAPERLESS_API_TOKEN: string;
}

const EMPTY_FORM: FormState = {
  HA_ENABLED: false,
  HA_URL: '',
  HA_TOKEN: '',
  PAPERLESS_ENABLED: false,
  PAPERLESS_URL: '',
  PAPERLESS_API_TOKEN: '',
};

// Credential keys per service (changes to these invalidate that service's
// test result). The ENABLED bool is UI-only and doesn't change what would
// be tested.
const HA_CRED_KEYS: ReadonlySet<keyof FormState> = new Set(HA_KEYS);
const PAPERLESS_CRED_KEYS: ReadonlySet<keyof FormState> = new Set(PAPERLESS_KEYS);

interface SecretsPresent {
  HA_TOKEN: boolean;
  PAPERLESS_API_TOKEN: boolean;
}

const INPUT =
  'w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-white focus:border-brand-500 focus:outline-none disabled:opacity-50';
const LABEL = 'mb-1.5 block text-xs uppercase tracking-wider text-zinc-500';
const BTN_PRIMARY =
  'inline-flex items-center gap-2 rounded-lg bg-brand-600 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-brand-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-50';
const BTN_SECONDARY =
  'inline-flex items-center gap-2 rounded-md border border-zinc-700 px-4 py-2 text-sm text-zinc-300 transition-colors hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-40';
const BTN_TEST =
  'inline-flex items-center gap-2 rounded-md border border-brand-500/40 bg-brand-500/10 px-3 py-1.5 text-xs font-medium text-brand-300 transition-colors hover:bg-brand-500/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-40';

type TestState = { status: 'idle' | 'pending' | 'ok' | 'fail'; detail?: string };

export default function IntegrationsStep({
  onNext,
  onBack,
}: IntegrationsStepProps) {
  const [draft, setDraft] = useState<FormState>(EMPTY_FORM);
  const [original, setOriginal] = useState<FormState>(EMPTY_FORM);
  const [secrets, setSecrets] = useState<SecretsPresent>({
    HA_TOKEN: false,
    PAPERLESS_API_TOKEN: false,
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [haTest, setHaTest] = useState<TestState>({ status: 'idle' });
  const [paperlessTest, setPaperlessTest] = useState<TestState>({ status: 'idle' });
  const [showSecret, setShowSecret] = useState({
    HA_TOKEN: false,
    PAPERLESS_API_TOKEN: false,
  });

  useEffect(() => {
    let cancelled = false;
    setupApi
      .getEnv()
      .then((env) => {
        if (cancelled) return;
        const haUrl = env.keys.HA_URL?.value ?? '';
        const haTokenPresent = env.keys.HA_TOKEN?.present ?? false;
        const paperlessUrl = env.keys.PAPERLESS_URL?.value ?? '';
        const paperlessTokenPresent =
          env.keys.PAPERLESS_API_TOKEN?.present ?? false;
        const next: FormState = {
          ...EMPTY_FORM,
          HA_URL: haUrl,
          // The integration is "on" in UI terms if either credential is
          // already configured — gives a sensible default on re-entry.
          HA_ENABLED: haUrl !== '' || haTokenPresent,
          PAPERLESS_URL: paperlessUrl,
          PAPERLESS_ENABLED: paperlessUrl !== '' || paperlessTokenPresent,
        };
        setSecrets({
          HA_TOKEN: haTokenPresent,
          PAPERLESS_API_TOKEN: paperlessTokenPresent,
        });
        setDraft(next);
        setOriginal(next);
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

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setDraft((d) => ({ ...d, [key]: value }));
    if (HA_CRED_KEYS.has(key)) setHaTest({ status: 'idle' });
    if (PAPERLESS_CRED_KEYS.has(key)) setPaperlessTest({ status: 'idle' });
  }

  async function testHA() {
    setError('');
    const url = draft.HA_URL.trim();
    const token = draft.HA_TOKEN.trim();
    if (!url || !token) {
      const missing: string[] = [];
      if (!url) missing.push('HA_URL');
      if (!token)
        missing.push(
          secrets.HA_TOKEN ? 'HA_TOKEN (re-type to test the stored value)' : 'HA_TOKEN',
        );
      setHaTest({ status: 'fail', detail: `Missing: ${missing.join(', ')}.` });
      return;
    }
    setHaTest({ status: 'pending' });
    try {
      const r = await setupApi.validateEnv('ha', {
        HA_URL: url,
        HA_TOKEN: token,
      });
      setHaTest({ status: r.ok ? 'ok' : 'fail', detail: r.detail });
    } catch (e) {
      setHaTest({
        status: 'fail',
        detail: e instanceof Error ? e.message : 'Test failed',
      });
    }
  }

  async function testPaperless() {
    setError('');
    const url = draft.PAPERLESS_URL.trim();
    const token = draft.PAPERLESS_API_TOKEN.trim();
    if (!url || !token) {
      const missing: string[] = [];
      if (!url) missing.push('PAPERLESS_URL');
      if (!token)
        missing.push(
          secrets.PAPERLESS_API_TOKEN
            ? 'PAPERLESS_API_TOKEN (re-type to test the stored value)'
            : 'PAPERLESS_API_TOKEN',
        );
      setPaperlessTest({
        status: 'fail',
        detail: `Missing: ${missing.join(', ')}.`,
      });
      return;
    }
    setPaperlessTest({ status: 'pending' });
    try {
      const r = await setupApi.validateEnv('paperless', {
        PAPERLESS_URL: url,
        PAPERLESS_API_TOKEN: token,
      });
      setPaperlessTest({ status: r.ok ? 'ok' : 'fail', detail: r.detail });
    } catch (e) {
      setPaperlessTest({
        status: 'fail',
        detail: e instanceof Error ? e.message : 'Test failed',
      });
    }
  }

  function buildWrites(): Record<string, string> {
    // Write only the credential keys, only if non-empty and changed. ENABLED
    // bools are UI-only and not part of the allow-list.
    const writes: Record<string, string> = {};
    for (const k of ALL_KEYS) {
      const v = (draft[k] as string).trim();
      if (v === '') continue;
      if (v !== original[k]) writes[k] = v;
    }
    return writes;
  }

  /** Same broken-config guard as PushStep: if a user toggled an integration On
   *  but only filled half the credentials, warn. Counts stored-but-not-shown
   *  secrets as "present" so a returning user with a saved token doesn't get
   *  blocked when they haven't re-typed it. */
  function missingRequired(): { ha: string[]; paperless: string[] } {
    const ha: string[] = [];
    if (draft.HA_ENABLED) {
      if (!draft.HA_URL.trim()) ha.push('HA_URL');
      if (!draft.HA_TOKEN.trim() && !secrets.HA_TOKEN) ha.push('HA_TOKEN');
    }
    const paperless: string[] = [];
    if (draft.PAPERLESS_ENABLED) {
      if (!draft.PAPERLESS_URL.trim()) paperless.push('PAPERLESS_URL');
      if (!draft.PAPERLESS_API_TOKEN.trim() && !secrets.PAPERLESS_API_TOKEN)
        paperless.push('PAPERLESS_API_TOKEN');
    }
    return { ha, paperless };
  }

  async function handleNext() {
    setError('');
    const writes = buildWrites();
    if (Object.keys(writes).length === 0) {
      onNext();
      return;
    }
    const missing = missingRequired();
    const broken: string[] = [];
    if (missing.ha.length > 0)
      broken.push(
        `Home Assistant is on but ${missing.ha.join(' + ')} ${missing.ha.length === 1 ? 'is' : 'are'} empty`,
      );
    if (missing.paperless.length > 0)
      broken.push(
        `Paperless is on but ${missing.paperless.join(' + ')} ${missing.paperless.length === 1 ? 'is' : 'are'} empty`,
      );
    if (broken.length > 0) {
      setError(
        `${broken.join('; ')}. Fill the missing fields, or turn the integration Off.`,
      );
      return;
    }
    setSaving(true);
    try {
      await setupApi.setEnv(writes);
      onNext();
    } catch (e) {
      setError(
        e instanceof Error ? e.message : 'Could not save integration settings',
      );
    } finally {
      setSaving(false);
    }
  }

  function handleSkip() {
    const writes = buildWrites();
    if (
      Object.keys(writes).length > 0 &&
      !window.confirm(
        'You have unsaved integration settings. Skip and discard them?',
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
        <h2 className="text-xl font-semibold text-white">Optional integrations</h2>
        <p className="text-sm text-zinc-400">
          Connect Jess to other services you already run. Both are optional —
          skip if you don’t use them; you can always add them later in Settings.
        </p>
      </div>

      {loadError && (
        <div className="rounded-lg border border-red-900/40 bg-red-950/30 p-3 text-sm text-red-300">
          Couldn’t load current settings: {loadError}. You can still pick
          values below.
        </div>
      )}

      <IntegrationCard
        title="Home Assistant"
        blurb="Unlocks the home_assistant tool (lights, locks, sensors, presence). The token is a Long-Lived Access Token from your HA profile."
        enabled={draft.HA_ENABLED}
        onToggle={(v) => set('HA_ENABLED', v)}
        testButton={
          <TestButton
            state={haTest}
            onClick={testHA}
            disabled={saving || !draft.HA_ENABLED}
          />
        }
      >
        <div className="space-y-3">
          <Field
            id="setup-ha-url"
            label="Home Assistant URL"
            value={draft.HA_URL}
            onChange={(v) => set('HA_URL', v)}
            placeholder="http://homeassistant.local:8123"
            disabled={!draft.HA_ENABLED}
          />
          <SecretField
            id="setup-ha-token"
            label="Long-lived access token"
            value={draft.HA_TOKEN}
            onChange={(v) => set('HA_TOKEN', v)}
            stored={secrets.HA_TOKEN}
            show={showSecret.HA_TOKEN}
            onToggleShow={() =>
              setShowSecret((s) => ({ ...s, HA_TOKEN: !s.HA_TOKEN }))
            }
            disabled={!draft.HA_ENABLED}
          />
        </div>
      </IntegrationCard>

      <IntegrationCard
        title="Paperless-ngx"
        blurb="Pushes files from /app/data/paperless_inbox to Paperless for OCR + auto-tagging via the paperless_save tool."
        enabled={draft.PAPERLESS_ENABLED}
        onToggle={(v) => set('PAPERLESS_ENABLED', v)}
        testButton={
          <TestButton
            state={paperlessTest}
            onClick={testPaperless}
            disabled={saving || !draft.PAPERLESS_ENABLED}
          />
        }
      >
        <div className="space-y-3">
          <Field
            id="setup-paperless-url"
            label="Paperless-ngx URL"
            value={draft.PAPERLESS_URL}
            onChange={(v) => set('PAPERLESS_URL', v)}
            placeholder="http://paperless.example.com:8777"
            disabled={!draft.PAPERLESS_ENABLED}
          />
          <SecretField
            id="setup-paperless-token"
            label="API token"
            value={draft.PAPERLESS_API_TOKEN}
            onChange={(v) => set('PAPERLESS_API_TOKEN', v)}
            stored={secrets.PAPERLESS_API_TOKEN}
            show={showSecret.PAPERLESS_API_TOKEN}
            onToggleShow={() =>
              setShowSecret((s) => ({
                ...s,
                PAPERLESS_API_TOKEN: !s.PAPERLESS_API_TOKEN,
              }))
            }
            disabled={!draft.PAPERLESS_ENABLED}
          />
        </div>
      </IntegrationCard>

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

// ---------------- presentational sub-components ----------------
// These mirror PushStep's ChannelCard / Field / SecretField / TestButton.
// TODO(post-merge): extract to `setup/wizard-fields.tsx` and import in both
// step components — both copies must stay in sync until then.

interface IntegrationCardProps {
  title: string;
  blurb: string;
  enabled: boolean;
  onToggle: (v: boolean) => void;
  testButton: React.ReactNode;
  children: React.ReactNode;
}

function IntegrationCard({
  title,
  blurb,
  enabled,
  onToggle,
  testButton,
  children,
}: IntegrationCardProps) {
  return (
    <div
      className={`rounded-lg border p-4 ${enabled ? 'border-zinc-700' : 'border-zinc-800'}`}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-3">
          <Plug
            size={16}
            className={`mt-0.5 shrink-0 ${enabled ? 'text-brand-400' : 'text-zinc-500'}`}
          />
          <div className="min-w-0">
            <p
              className={`text-sm font-semibold ${enabled ? 'text-white' : 'text-zinc-500'}`}
            >
              {title}
            </p>
            <p className="mt-0.5 text-xs text-zinc-500">{blurb}</p>
          </div>
        </div>
        <label className="flex shrink-0 cursor-pointer items-center gap-2">
          <span className="w-6 text-right text-xs text-zinc-400">
            {enabled ? 'On' : 'Off'}
          </span>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => onToggle(e.target.checked)}
            className="h-4 w-4 accent-brand-600"
            aria-label={`${title} enabled`}
          />
        </label>
      </div>

      {enabled && (
        <div className="mt-4 space-y-4 border-t border-zinc-800 pt-4">
          {children}
          <div className="flex items-center justify-end">{testButton}</div>
        </div>
      )}
    </div>
  );
}

function Field({
  id,
  label,
  value,
  onChange,
  placeholder,
  disabled,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  return (
    <div>
      <label className={LABEL} htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className={INPUT}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        spellCheck={false}
        autoComplete="off"
      />
    </div>
  );
}

function SecretField({
  id,
  label,
  value,
  onChange,
  stored,
  show,
  onToggleShow,
  disabled,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  stored: boolean;
  show: boolean;
  onToggleShow: () => void;
  disabled?: boolean;
}) {
  return (
    <div>
      <label className={LABEL} htmlFor={id}>
        {label}
      </label>
      <div className="flex gap-2">
        <input
          id={id}
          className={INPUT + ' font-mono'}
          type={show ? 'text' : 'password'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={stored ? '•••••••• (saved — type to replace)' : ''}
          disabled={disabled}
          spellCheck={false}
          autoComplete="off"
        />
        <button
          type="button"
          onClick={onToggleShow}
          disabled={disabled || value === ''}
          className="shrink-0 rounded-md border border-zinc-700 px-3 text-zinc-400 transition-colors hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-40"
          aria-label={show ? 'Hide value' : 'Show value'}
        >
          {show ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
      </div>
    </div>
  );
}

function TestButton({
  state,
  onClick,
  disabled,
}: {
  state: TestState;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-center gap-3">
      {state.status === 'ok' && (
        <span
          role="status"
          aria-live="polite"
          className="inline-flex items-center gap-1.5 text-xs text-emerald-400"
        >
          <CheckCircle2 size={14} aria-hidden="true" />
          <span>{state.detail ?? 'OK'}</span>
        </span>
      )}
      {state.status === 'fail' && (
        <span
          role="status"
          aria-live="polite"
          className="inline-flex items-center gap-1.5 text-xs text-red-400"
        >
          <AlertCircle size={14} aria-hidden="true" />
          <span>{state.detail ?? 'Failed'}</span>
        </span>
      )}
      <button
        type="button"
        onClick={onClick}
        disabled={disabled || state.status === 'pending'}
        aria-busy={state.status === 'pending'}
        className={BTN_TEST}
      >
        {state.status === 'pending' && (
          <Loader2 size={12} className="animate-spin" aria-hidden="true" />
        )}
        Test connection
      </button>
    </div>
  );
}
