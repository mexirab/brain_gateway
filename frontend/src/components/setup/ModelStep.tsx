'use client';

import { useEffect, useMemo, useState } from 'react';
import { Loader2, Cpu, Wand2, ChevronDown, ChevronUp } from 'lucide-react';
import {
  setupApi,
  type EnvStatus,
  type HardwareScanData,
} from '@/lib/setup-api';

interface ModelStepProps {
  onNext: () => void;
  onBack: () => void;
}

// Field keys this step writes. Order matters for rendering.
const FIELD_KEYS = [
  'VLLM_MODEL',
  'VLLM_QUANTIZATION',
  'VLLM_SERVED_NAME',
  'VLLM_MAX_MODEL_LEN',
  'VLLM_GPU_MEM_UTIL',
  'MODEL_NAME',
] as const;
type FieldKey = (typeof FIELD_KEYS)[number];

type FormState = Record<FieldKey, string>;

const EMPTY_FORM: FormState = {
  VLLM_MODEL: '',
  VLLM_QUANTIZATION: '',
  VLLM_SERVED_NAME: '',
  VLLM_MAX_MODEL_LEN: '',
  VLLM_GPU_MEM_UTIL: '',
  MODEL_NAME: '',
};

const QUANTIZATION_OPTIONS = [
  { value: '', label: '— select —' },
  { value: 'auto_round', label: 'auto_round (AutoRound int4 models)' },
  { value: 'awq', label: 'awq (AWQ-quantized models)' },
  { value: 'auto', label: 'auto (detect from config)' },
];

const INPUT =
  'w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-white focus:border-brand-500 focus:outline-none disabled:opacity-50';
const LABEL = 'mb-1.5 block text-xs uppercase tracking-wider text-zinc-500';
const BTN_PRIMARY =
  'inline-flex items-center gap-2 rounded-lg bg-brand-600 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-brand-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-50';
const BTN_SECONDARY =
  'inline-flex items-center gap-2 rounded-md border border-zinc-700 px-4 py-2 text-sm text-zinc-300 transition-colors hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-40';

/** Derive a sane VLLM_SERVED_NAME from a HuggingFace repo id.
 *  "Lorbus/Qwen3.6-27B-int4-AutoRound" -> "qwen3.6-27b-int4-autoround".
 *  Restricted to [a-z0-9._-], trimmed, capped at 64 chars — vLLM is permissive
 *  here but the orchestrator's MODEL_NAME has to match exactly downstream. */
function deriveServedName(modelId: string): string {
  const basename = (modelId.split('/').pop() ?? modelId).trim();
  return basename
    .toLowerCase()
    .replace(/[^a-z0-9._-]/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64);
}

/** Validate a numeric env value with a domain check. Returns the error message
 *  or null when valid. Empty is treated as "no value provided" and OK here —
 *  the dirty-diff in handleNext won't try to write an empty value anyway. */
function validateNumeric(
  key: 'VLLM_MAX_MODEL_LEN' | 'VLLM_GPU_MEM_UTIL',
  raw: string,
): string | null {
  const v = raw.trim();
  if (v === '') return null;
  if (key === 'VLLM_MAX_MODEL_LEN') {
    if (!/^\d+$/.test(v)) return 'must be a positive integer';
    if (Number(v) < 1) return 'must be a positive integer';
    return null;
  }
  // VLLM_GPU_MEM_UTIL: float in (0, 1].
  const f = Number(v);
  if (!Number.isFinite(f)) return 'must be a number';
  if (f <= 0 || f > 1) return 'must be between 0 (exclusive) and 1';
  return null;
}

/** Map a HardwareScanData.recommendation block to FormState. Null if the scan
 *  has no model recommendation (e.g. below-floor hardware). */
function recommendationToForm(scan: HardwareScanData): FormState | null {
  const rec = scan.recommendation;
  if (!rec || !rec.model) return null;
  const served = deriveServedName(rec.model);
  return {
    VLLM_MODEL: rec.model,
    VLLM_QUANTIZATION: rec.quantization ?? '',
    VLLM_SERVED_NAME: served,
    VLLM_MAX_MODEL_LEN: String(rec.max_model_len),
    VLLM_GPU_MEM_UTIL: String(rec.gpu_mem_util),
    MODEL_NAME: served,
  };
}

function envToForm(env: EnvStatus): FormState {
  const out = { ...EMPTY_FORM };
  for (const k of FIELD_KEYS) {
    const v = env.keys[k]?.value;
    if (typeof v === 'string') out[k] = v;
  }
  return out;
}

export default function ModelStep({ onNext, onBack }: ModelStepProps) {
  const [scan, setScan] = useState<HardwareScanData | null>(null);
  const [draft, setDraft] = useState<FormState>(EMPTY_FORM);
  const [original, setOriginal] = useState<FormState>(EMPTY_FORM);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [advancedOpen, setAdvancedOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([setupApi.getHardware(), setupApi.getEnv()])
      .then(([hw, env]) => {
        if (cancelled) return;
        const scanData = hw.available && hw.scan ? hw.scan : null;
        setScan(scanData);
        const fromEnv = envToForm(env);
        // If no env override is set yet AND a recommendation exists, pre-fill
        // the form with the recommendation so the user sees concrete values.
        const anyEnvSet = FIELD_KEYS.some((k) => fromEnv[k] !== '');
        const fromRec = scanData ? recommendationToForm(scanData) : null;
        const initial = !anyEnvSet && fromRec ? fromRec : fromEnv;
        setDraft(initial);
        setOriginal(fromEnv);
        // Auto-open advanced if any of the advanced fields is non-empty.
        if (
          initial.VLLM_SERVED_NAME ||
          initial.VLLM_MAX_MODEL_LEN ||
          initial.VLLM_GPU_MEM_UTIL ||
          initial.MODEL_NAME
        ) {
          setAdvancedOpen(true);
        }
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

  const recommendation = useMemo(
    () => (scan ? recommendationToForm(scan) : null),
    [scan],
  );

  function applyRecommendation() {
    if (!recommendation) return;
    // Don't blow away anything the user has already typed: only fill empty
    // fields. To fully overwrite, the user can clear the fields first.
    setDraft((d) => {
      const next: FormState = { ...d };
      for (const k of FIELD_KEYS) {
        if (next[k] === '') next[k] = recommendation[k];
      }
      return next;
    });
    setAdvancedOpen(true);
    setError('');
  }

  function set(key: FieldKey, value: string) {
    setDraft((d) => ({ ...d, [key]: value }));
  }

  async function handleNext() {
    setError('');
    // Range-check the numeric fields client-side. The backend's allow-list
    // accepts any string; a bogus value would only surface as a vLLM boot
    // failure post-restart — much worse UX than catching it here.
    for (const k of ['VLLM_MAX_MODEL_LEN', 'VLLM_GPU_MEM_UTIL'] as const) {
      const err = validateNumeric(k, draft[k]);
      if (err) {
        setError(`${k}: ${err}`);
        setAdvancedOpen(true);
        return;
      }
    }
    // If exactly one of MODEL_NAME / VLLM_SERVED_NAME is set, mirror it — they
    // must match (orchestrator-side name == vLLM --served-model-name) and the
    // "type one, forget the other" gap is the #1 first-boot bricker.
    const draftMirrored = { ...draft };
    if (draftMirrored.VLLM_SERVED_NAME && !draftMirrored.MODEL_NAME) {
      draftMirrored.MODEL_NAME = draftMirrored.VLLM_SERVED_NAME;
    } else if (draftMirrored.MODEL_NAME && !draftMirrored.VLLM_SERVED_NAME) {
      draftMirrored.VLLM_SERVED_NAME = draftMirrored.MODEL_NAME;
    }
    // Write only fields that differ from what was loaded AND are non-empty.
    // Empty + previously-set fields would require DELETE — out of scope for
    // this slice; users wanting to unset a key edit /api/setup/env directly.
    const toWrite: Record<string, string> = {};
    for (const k of FIELD_KEYS) {
      const v = draftMirrored[k].trim();
      if (v !== '' && v !== original[k]) toWrite[k] = v;
    }
    if (Object.keys(toWrite).length === 0) {
      onNext();
      return;
    }
    setSaving(true);
    try {
      await setupApi.setEnv(toWrite);
      onNext();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save model settings');
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

  const isDirty = FIELD_KEYS.some((k) => draft[k] !== original[k]);

  function handleSkip() {
    if (
      isDirty &&
      !window.confirm(
        'You have unsaved model settings. Skip and discard them?',
      )
    ) {
      return;
    }
    onNext();
  }

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h2 className="text-xl font-semibold text-white">Primary model</h2>
        <p className="text-sm text-zinc-400">
          Pick the LLM Jess will use for chat and tool calls. Skip if you’re
          happy with the built-in defaults.
        </p>
      </div>

      {loadError && (
        <div className="rounded-lg border border-red-900/40 bg-red-950/30 p-3 text-sm text-red-300">
          Couldn’t load current settings: {loadError}. You can still pick a
          model below.
        </div>
      )}

      {/* Hardware + recommendation card */}
      {scan ? (
        <div className="rounded-lg border border-zinc-700 bg-zinc-900/40 p-4 space-y-3 text-sm">
          <div className="flex items-start gap-3">
            <Cpu size={16} className="mt-0.5 shrink-0 text-emerald-400" />
            <div className="space-y-1">
              <p className="text-zinc-200">
                Detected {scan.gpu_count} GPU
                {scan.gpu_count === 1 ? '' : 's'} — largest {scan.largest_gpu_gib}{' '}
                GiB
                {scan.vram_tier ? ` (${scan.vram_tier} GB tier)` : ''}.
              </p>
              <p className="text-xs text-zinc-500">
                {scan.gpus
                  .map((g) => `${g.name} (${g.vram_gib} GiB)`)
                  .join(', ')}
              </p>
            </div>
          </div>
          {recommendation ? (
            <div className="flex flex-wrap items-center gap-3 border-t border-zinc-800 pt-3">
              <div className="min-w-0 flex-1">
                <p className="text-xs uppercase tracking-wider text-zinc-500">
                  Recommended model
                </p>
                <p
                  className="break-all font-mono text-zinc-200"
                  title={recommendation.VLLM_MODEL}
                >
                  {recommendation.VLLM_MODEL}
                </p>
              </div>
              <button
                type="button"
                onClick={applyRecommendation}
                disabled={saving}
                className={BTN_SECONDARY}
              >
                <Wand2 size={14} />
                Use this
              </button>
            </div>
          ) : (
            <p className="border-t border-zinc-800 pt-3 text-xs text-zinc-500">
              Your hardware is below the recommended floor — pick a model
              manually below (e.g.{' '}
              <code className="text-zinc-300">Qwen/Qwen3-8B-AWQ</code> for a
              ~16 GB card,{' '}
              <code className="text-zinc-300">Qwen/Qwen3-14B-Instruct-AWQ</code>{' '}
              for ~24 GB), or skip this step and adjust later in your{' '}
              <code className="text-zinc-300">.env</code>.
            </p>
          )}
        </div>
      ) : (
        <div className="flex items-start gap-3 rounded-lg border border-zinc-800 p-4 text-sm">
          <Cpu size={16} className="mt-0.5 shrink-0 text-amber-400" />
          <span className="text-zinc-400">
            No hardware scan found. You can run{' '}
            <code className="text-zinc-300">
              scripts/detect_hardware.sh --json
            </code>{' '}
            on the host later for a recommendation, or fill the model fields
            manually below.
          </span>
        </div>
      )}

      {/* Primary fields */}
      <div className="space-y-4">
        <div>
          <label className={LABEL} htmlFor="setup-vllm-model">
            HuggingFace model id
          </label>
          <input
            id="setup-vllm-model"
            className={INPUT}
            value={draft.VLLM_MODEL}
            onChange={(e) => set('VLLM_MODEL', e.target.value)}
            placeholder="e.g. Lorbus/Qwen3.6-27B-int4-AutoRound"
            spellCheck={false}
            autoComplete="off"
          />
        </div>

        <div>
          <label className={LABEL} htmlFor="setup-vllm-quant">
            Quantization
          </label>
          <select
            id="setup-vllm-quant"
            className={INPUT}
            value={draft.VLLM_QUANTIZATION}
            onChange={(e) => set('VLLM_QUANTIZATION', e.target.value)}
          >
            {QUANTIZATION_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Advanced disclosure */}
      <div className="rounded-lg border border-zinc-800">
        <button
          type="button"
          onClick={() => setAdvancedOpen((o) => !o)}
          className="flex w-full items-center justify-between rounded-lg px-4 py-2.5 text-sm text-zinc-300 hover:bg-zinc-900/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
          aria-expanded={advancedOpen}
          aria-controls="setup-vllm-advanced"
        >
          <span>Advanced settings</span>
          {advancedOpen ? (
            <ChevronUp size={16} className="text-zinc-500" />
          ) : (
            <ChevronDown size={16} className="text-zinc-500" />
          )}
        </button>
        {advancedOpen && (
          <div
            id="setup-vllm-advanced"
            className="space-y-4 border-t border-zinc-800 p-4"
          >
            <div>
              <label className={LABEL} htmlFor="setup-vllm-served-name">
                Served name (vLLM <code>--served-model-name</code>)
              </label>
              <input
                id="setup-vllm-served-name"
                className={INPUT}
                value={draft.VLLM_SERVED_NAME}
                onChange={(e) => set('VLLM_SERVED_NAME', e.target.value)}
                placeholder="e.g. qwen3.6-27b-int4"
                spellCheck={false}
                autoComplete="off"
              />
            </div>
            <div>
              <label className={LABEL} htmlFor="setup-model-name">
                Orchestrator model name (must match served name)
              </label>
              <input
                id="setup-model-name"
                className={INPUT}
                value={draft.MODEL_NAME}
                onChange={(e) => set('MODEL_NAME', e.target.value)}
                placeholder="e.g. qwen3.6-27b-int4"
                spellCheck={false}
                autoComplete="off"
              />
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <label className={LABEL} htmlFor="setup-vllm-maxlen">
                  Max model length (context window)
                </label>
                <input
                  id="setup-vllm-maxlen"
                  className={INPUT}
                  value={draft.VLLM_MAX_MODEL_LEN}
                  onChange={(e) => set('VLLM_MAX_MODEL_LEN', e.target.value)}
                  placeholder="153600"
                  inputMode="numeric"
                  pattern="[0-9]*"
                />
              </div>
              <div>
                <label className={LABEL} htmlFor="setup-vllm-gpumem">
                  GPU memory utilization (0–1)
                </label>
                <input
                  id="setup-vllm-gpumem"
                  className={INPUT}
                  value={draft.VLLM_GPU_MEM_UTIL}
                  onChange={(e) => set('VLLM_GPU_MEM_UTIL', e.target.value)}
                  placeholder="0.93"
                  inputMode="decimal"
                />
              </div>
            </div>
          </div>
        )}
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      {/* Footer */}
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
