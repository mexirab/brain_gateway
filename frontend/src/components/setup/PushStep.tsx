'use client';

import { useEffect, useState } from 'react';
import {
  Loader2,
  Bell,
  CheckCircle2,
  AlertCircle,
  Eye,
  EyeOff,
} from 'lucide-react';
import { setupApi } from '@/lib/setup-api';

interface PushStepProps {
  onNext: () => void;
  onBack: () => void;
}

// All push-related allow-listed keys (matches orchestrator/setup_env.py).
const NTFY_KEYS = [
  'NTFY_ENABLED',
  'NTFY_URL',
  'NTFY_TOPIC',
  'NTFY_HMAC_SECRET',
] as const;
const PUSHOVER_KEYS = [
  'PUSHOVER_ENABLED',
  'PUSHOVER_USER_KEY',
  'PUSHOVER_APP_TOKEN',
] as const;
const ALL_KEYS = [...NTFY_KEYS, ...PUSHOVER_KEYS] as const;

// Credential keys per channel (i.e. keys whose change should invalidate that
// channel's test result). Excludes the ENABLED bool — toggling on/off doesn't
// change what would be tested.
const NTFY_CRED_KEYS: ReadonlySet<keyof FormState> = new Set([
  'NTFY_URL',
  'NTFY_TOPIC',
  'NTFY_HMAC_SECRET',
]);
const PUSHOVER_CRED_KEYS: ReadonlySet<keyof FormState> = new Set([
  'PUSHOVER_USER_KEY',
  'PUSHOVER_APP_TOKEN',
]);

// Form state: ENABLED kept as boolean for UX; converted to "true"/"false"
// at the API boundary because the env-overrides backend stores strings.
interface FormState {
  NTFY_ENABLED: boolean;
  NTFY_URL: string;
  NTFY_TOPIC: string;
  NTFY_HMAC_SECRET: string;
  PUSHOVER_ENABLED: boolean;
  PUSHOVER_USER_KEY: string;
  PUSHOVER_APP_TOKEN: string;
}

const EMPTY_FORM: FormState = {
  NTFY_ENABLED: false,
  NTFY_URL: '',
  NTFY_TOPIC: '',
  NTFY_HMAC_SECRET: '',
  PUSHOVER_ENABLED: false,
  PUSHOVER_USER_KEY: '',
  PUSHOVER_APP_TOKEN: '',
};

// Tracks whether a secret key is "saved-but-not-shown" — the backend returns
// `present: true` without echoing the value, so we can't re-render it. We
// surface this in the input placeholder so the user knows something is there.
interface SecretsPresent {
  NTFY_HMAC_SECRET: boolean;
  PUSHOVER_USER_KEY: boolean;
  PUSHOVER_APP_TOKEN: boolean;
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

function asBool(v: string | undefined): boolean {
  if (!v) return false;
  return v.trim().toLowerCase() === 'true' || v.trim() === '1';
}

export default function PushStep({ onNext, onBack }: PushStepProps) {
  const [draft, setDraft] = useState<FormState>(EMPTY_FORM);
  const [original, setOriginal] = useState<FormState>(EMPTY_FORM);
  const [secrets, setSecrets] = useState<SecretsPresent>({
    NTFY_HMAC_SECRET: false,
    PUSHOVER_USER_KEY: false,
    PUSHOVER_APP_TOKEN: false,
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [ntfyTest, setNtfyTest] = useState<TestState>({ status: 'idle' });
  const [pushoverTest, setPushoverTest] = useState<TestState>({ status: 'idle' });
  const [showSecret, setShowSecret] = useState({
    NTFY_HMAC_SECRET: false,
    PUSHOVER_USER_KEY: false,
    PUSHOVER_APP_TOKEN: false,
  });

  useEffect(() => {
    let cancelled = false;
    setupApi
      .getEnv()
      .then((env) => {
        if (cancelled) return;
        const next: FormState = { ...EMPTY_FORM };
        next.NTFY_ENABLED = asBool(env.keys.NTFY_ENABLED?.value);
        next.NTFY_URL = env.keys.NTFY_URL?.value ?? '';
        next.NTFY_TOPIC = env.keys.NTFY_TOPIC?.value ?? '';
        next.PUSHOVER_ENABLED = asBool(env.keys.PUSHOVER_ENABLED?.value);
        // Secret values are NEVER returned. We only learn whether they're set.
        setSecrets({
          NTFY_HMAC_SECRET: env.keys.NTFY_HMAC_SECRET?.present ?? false,
          PUSHOVER_USER_KEY: env.keys.PUSHOVER_USER_KEY?.present ?? false,
          PUSHOVER_APP_TOKEN: env.keys.PUSHOVER_APP_TOKEN?.present ?? false,
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
    // Invalidate the channel's test result when a CREDENTIAL key changes.
    // Toggling ENABLED doesn't change what would be tested, so leave the
    // result alone — the user can re-test if they want.
    if (NTFY_CRED_KEYS.has(key)) setNtfyTest({ status: 'idle' });
    if (PUSHOVER_CRED_KEYS.has(key)) setPushoverTest({ status: 'idle' });
  }

  async function testNtfy() {
    setError('');
    const topic = draft.NTFY_TOPIC.trim();
    if (!draft.NTFY_URL.trim() || !topic) {
      setNtfyTest({
        status: 'fail',
        detail: 'NTFY_URL and NTFY_TOPIC are both required.',
      });
      return;
    }
    // ntfy's test is a real publish — every subscribed device buzzes. Warn
    // before doing it (the backend validator's docstring explicitly asks the
    // UI to do this — see orchestrator/setup_env.py:_validate_ntfy).
    if (
      !window.confirm(
        `This will publish a "setup wizard test" notification to the ntfy topic "${topic}". Anyone subscribed (including other devices) will receive the push. Continue?`,
      )
    ) {
      return;
    }
    setNtfyTest({ status: 'pending' });
    try {
      const r = await setupApi.validateEnv('ntfy', {
        NTFY_URL: draft.NTFY_URL.trim(),
        NTFY_TOPIC: topic,
      });
      setNtfyTest({ status: r.ok ? 'ok' : 'fail', detail: r.detail });
    } catch (e) {
      setNtfyTest({
        status: 'fail',
        detail: e instanceof Error ? e.message : 'Test failed',
      });
    }
  }

  async function testPushover() {
    setError('');
    // Live test requires the actual secrets in the form. If they're "saved"
    // (present on the server) but the user hasn't re-typed them, we can't
    // test — surface that explicitly.
    const userKey = draft.PUSHOVER_USER_KEY.trim();
    const appToken = draft.PUSHOVER_APP_TOKEN.trim();
    if (!userKey || !appToken) {
      const missing = [];
      if (!userKey) missing.push(secrets.PUSHOVER_USER_KEY ? 'PUSHOVER_USER_KEY (re-type to test the stored value)' : 'PUSHOVER_USER_KEY');
      if (!appToken) missing.push(secrets.PUSHOVER_APP_TOKEN ? 'PUSHOVER_APP_TOKEN (re-type to test the stored value)' : 'PUSHOVER_APP_TOKEN');
      setPushoverTest({
        status: 'fail',
        detail: `Missing: ${missing.join(', ')}.`,
      });
      return;
    }
    setPushoverTest({ status: 'pending' });
    try {
      const r = await setupApi.validateEnv('pushover', {
        PUSHOVER_USER_KEY: userKey,
        PUSHOVER_APP_TOKEN: appToken,
      });
      setPushoverTest({ status: r.ok ? 'ok' : 'fail', detail: r.detail });
    } catch (e) {
      setPushoverTest({
        status: 'fail',
        detail: e instanceof Error ? e.message : 'Test failed',
      });
    }
  }

  function buildWrites(): Record<string, string> {
    // For each key: only write if non-empty (for strings/secrets) and different
    // from the loaded value. ENABLED flags are bools — always include if
    // changed, encoded as "true"/"false".
    const writes: Record<string, string> = {};
    for (const k of ALL_KEYS) {
      if (k === 'NTFY_ENABLED' || k === 'PUSHOVER_ENABLED') {
        if (draft[k] !== original[k]) writes[k] = draft[k] ? 'true' : 'false';
        continue;
      }
      const v = (draft[k] as string).trim();
      if (v === '') continue; // empty = no-op; clearing a value isn't supported
      if (v !== original[k]) writes[k] = v;
    }
    return writes;
  }

  /** Names of required-but-missing fields per enabled channel. A field is
   *  "present" if it's typed in the form OR already saved on the server (for
   *  secrets we can't echo back). Empty list = the channel is properly
   *  configured to be enabled. */
  function missingRequired(): { ntfy: string[]; pushover: string[] } {
    const ntfy: string[] = [];
    if (draft.NTFY_ENABLED) {
      if (!draft.NTFY_URL.trim()) ntfy.push('NTFY_URL');
      if (!draft.NTFY_TOPIC.trim()) ntfy.push('NTFY_TOPIC');
    }
    const pushover: string[] = [];
    if (draft.PUSHOVER_ENABLED) {
      if (!draft.PUSHOVER_USER_KEY.trim() && !secrets.PUSHOVER_USER_KEY)
        pushover.push('PUSHOVER_USER_KEY');
      if (!draft.PUSHOVER_APP_TOKEN.trim() && !secrets.PUSHOVER_APP_TOKEN)
        pushover.push('PUSHOVER_APP_TOKEN');
    }
    return { ntfy, pushover };
  }

  async function handleNext() {
    setError('');
    const writes = buildWrites();
    if (Object.keys(writes).length === 0) {
      onNext();
      return;
    }
    // Block silent broken-config: a channel toggled On with required fields
    // missing would write *_ENABLED=true and every push would silently fail
    // forever. Force the user to either fill the fields or turn the channel
    // back Off.
    const missing = missingRequired();
    const broken: string[] = [];
    if (missing.ntfy.length > 0)
      broken.push(`ntfy is on but ${missing.ntfy.join(' + ')} ${missing.ntfy.length === 1 ? 'is' : 'are'} empty`);
    if (missing.pushover.length > 0)
      broken.push(
        `Pushover is on but ${missing.pushover.join(' + ')} ${missing.pushover.length === 1 ? 'is' : 'are'} empty`,
      );
    if (broken.length > 0) {
      setError(
        `${broken.join('; ')}. Fill the missing fields, or turn the channel Off.`,
      );
      return;
    }
    setSaving(true);
    try {
      await setupApi.setEnv(writes);
      onNext();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save push settings');
    } finally {
      setSaving(false);
    }
  }

  function handleSkip() {
    const writes = buildWrites();
    if (
      Object.keys(writes).length > 0 &&
      !window.confirm(
        'You have unsaved push-channel settings. Skip and discard them?',
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
        <h2 className="text-xl font-semibold text-white">Push notifications</h2>
        <p className="text-sm text-zinc-400">
          How Jess reaches your phone when you’re away from the dashboard. Both
          channels are optional — set up either, both, or neither.
        </p>
      </div>

      {loadError && (
        <div className="rounded-lg border border-red-900/40 bg-red-950/30 p-3 text-sm text-red-300">
          Couldn’t load current settings: {loadError}. You can still pick
          values below.
        </div>
      )}

      {/* ntfy */}
      <ChannelCard
        title="ntfy"
        blurb="Free, self-hostable push notifications. The wizard test sends a low-priority message to the topic."
        enabled={draft.NTFY_ENABLED}
        onToggle={(v) => set('NTFY_ENABLED', v)}
        testButton={
          <TestButton
            state={ntfyTest}
            onClick={testNtfy}
            disabled={saving || !draft.NTFY_ENABLED}
          />
        }
      >
        <div className="space-y-3">
          <Field
            id="setup-ntfy-url"
            label="Server URL"
            value={draft.NTFY_URL}
            onChange={(v) => set('NTFY_URL', v)}
            placeholder="https://ntfy.example.com"
            disabled={!draft.NTFY_ENABLED}
          />
          <Field
            id="setup-ntfy-topic"
            label="Topic"
            value={draft.NTFY_TOPIC}
            onChange={(v) => set('NTFY_TOPIC', v)}
            placeholder="jess-reminders"
            disabled={!draft.NTFY_ENABLED}
          />
          <SecretField
            id="setup-ntfy-hmac"
            label="HMAC secret (for Done/Snooze callbacks)"
            value={draft.NTFY_HMAC_SECRET}
            onChange={(v) => set('NTFY_HMAC_SECRET', v)}
            stored={secrets.NTFY_HMAC_SECRET}
            show={showSecret.NTFY_HMAC_SECRET}
            onToggleShow={() =>
              setShowSecret((s) => ({ ...s, NTFY_HMAC_SECRET: !s.NTFY_HMAC_SECRET }))
            }
            disabled={!draft.NTFY_ENABLED}
          />
        </div>
      </ChannelCard>

      {/* Pushover */}
      <ChannelCard
        title="Pushover"
        blurb="Paid native iOS/Android push. The wizard test verifies your credentials against api.pushover.net without sending a message."
        enabled={draft.PUSHOVER_ENABLED}
        onToggle={(v) => set('PUSHOVER_ENABLED', v)}
        testButton={
          <TestButton
            state={pushoverTest}
            onClick={testPushover}
            disabled={saving || !draft.PUSHOVER_ENABLED}
          />
        }
      >
        <div className="space-y-3">
          <SecretField
            id="setup-pushover-user"
            label="User key"
            value={draft.PUSHOVER_USER_KEY}
            onChange={(v) => set('PUSHOVER_USER_KEY', v)}
            stored={secrets.PUSHOVER_USER_KEY}
            show={showSecret.PUSHOVER_USER_KEY}
            onToggleShow={() =>
              setShowSecret((s) => ({
                ...s,
                PUSHOVER_USER_KEY: !s.PUSHOVER_USER_KEY,
              }))
            }
            disabled={!draft.PUSHOVER_ENABLED}
          />
          <SecretField
            id="setup-pushover-token"
            label="App token"
            value={draft.PUSHOVER_APP_TOKEN}
            onChange={(v) => set('PUSHOVER_APP_TOKEN', v)}
            stored={secrets.PUSHOVER_APP_TOKEN}
            show={showSecret.PUSHOVER_APP_TOKEN}
            onToggleShow={() =>
              setShowSecret((s) => ({
                ...s,
                PUSHOVER_APP_TOKEN: !s.PUSHOVER_APP_TOKEN,
              }))
            }
            disabled={!draft.PUSHOVER_ENABLED}
          />
        </div>
      </ChannelCard>

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

interface ChannelCardProps {
  title: string;
  blurb: string;
  enabled: boolean;
  onToggle: (v: boolean) => void;
  testButton: React.ReactNode;
  children: React.ReactNode;
}

function ChannelCard({
  title,
  blurb,
  enabled,
  onToggle,
  testButton,
  children,
}: ChannelCardProps) {
  return (
    <div
      className={`rounded-lg border p-4 ${enabled ? 'border-zinc-700' : 'border-zinc-800'}`}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-3">
          <Bell
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
        <span className="inline-flex items-center gap-1.5 text-xs text-emerald-400">
          <CheckCircle2 size={14} />
          <span>{state.detail ?? 'OK'}</span>
        </span>
      )}
      {state.status === 'fail' && (
        <span className="inline-flex items-center gap-1.5 text-xs text-red-400">
          <AlertCircle size={14} />
          <span>{state.detail ?? 'Failed'}</span>
        </span>
      )}
      <button
        type="button"
        onClick={onClick}
        disabled={disabled || state.status === 'pending'}
        className={BTN_TEST}
      >
        {state.status === 'pending' && (
          <Loader2 size={12} className="animate-spin" />
        )}
        Test connection
      </button>
    </div>
  );
}
