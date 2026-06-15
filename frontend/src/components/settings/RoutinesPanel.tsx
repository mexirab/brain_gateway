'use client';

import { useState, useEffect, useCallback } from 'react';
import { Loader2, Plus, Trash2, ArrowUp, ArrowDown } from 'lucide-react';
import {
  settingsApi,
  type Routine,
  type RoutineStep,
  type RoutinesPayload,
  type Weekday,
} from '@/lib/settings-api';
import { SaveBar } from './IdentityPanel';
import { Button } from '@/components/ui';

const DAY_LABELS: Array<{ key: Weekday; label: string }> = [
  { key: 'mon', label: 'Mon' },
  { key: 'tue', label: 'Tue' },
  { key: 'wed', label: 'Wed' },
  { key: 'thu', label: 'Thu' },
  { key: 'fri', label: 'Fri' },
  { key: 'sat', label: 'Sat' },
  { key: 'sun', label: 'Sun' },
];

import type { DirtyRegister } from '@/app/(private)/settings/page';

interface PanelProps {
  registerDirty?: DirtyRegister;
}

function newStep(existingIds: Set<string> = new Set()): RoutineStep {
  // id has to match `^[a-z0-9_]+$` server-side. Use 8 hex chars from
  // crypto.randomUUID (~3.4e9 range) + collision retry against the
  // current routine's step ids. id stays internal; label is what the
  // user edits + what TTS speaks.
  const gen = () => {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return `step_${crypto.randomUUID().replace(/-/g, '').slice(0, 8)}`;
    }
    return `step_${Math.random().toString(36).slice(2, 10)}`;
  };
  let id = gen();
  let attempts = 0;
  while (existingIds.has(id) && attempts < 20) {
    id = gen();
    attempts++;
  }
  return { id, label: 'New step', est_minutes: 5, skippable: true };
}

export default function RoutinesPanel({ registerDirty }: PanelProps = {}) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [original, setOriginal] = useState<RoutinesPayload | null>(null);
  const [draft, setDraft] = useState<RoutinesPayload | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeRoutineId, setActiveRoutineId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await settingsApi.getRoutines();
      setOriginal(data);
      setDraft(JSON.parse(JSON.stringify(data)));
      // Default the inner sub-tab to "morning" when present, else first key.
      const ids = Object.keys(data.routines);
      if (ids.length > 0) {
        setActiveRoutineId(ids.includes('morning') ? 'morning' : ids[0]);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load routines');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const dirty = draft !== null && original !== null && JSON.stringify(draft) !== JSON.stringify(original);

  useEffect(() => {
    registerDirty?.('routines', dirty);
  }, [dirty, registerDirty]);

  function patchRoutine(id: string, updater: (r: Routine) => Routine) {
    setDraft((d) => {
      if (!d) return d;
      const next = { ...d, routines: { ...d.routines } };
      next.routines[id] = updater(next.routines[id]);
      return next;
    });
    setStatusMsg(null);
  }

  async function handleSave() {
    if (!draft) return;
    setSaving(true);
    setError(null);
    setStatusMsg(null);
    try {
      const saved = await settingsApi.updateRoutines(draft);
      const stripped: RoutinesPayload = { routines: saved.routines };
      setOriginal(stripped);
      setDraft(JSON.parse(JSON.stringify(stripped)));
      const reload = saved._reload;
      if (reload?.reload_error) {
        setStatusMsg(`Saved, but rescheduling failed: ${reload.reload_error}`);
      } else {
        setStatusMsg(
          reload
            ? `Saved. Rescheduled ${reload.rescheduled.length} routine(s).`
            : 'Saved.',
        );
      }
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

  const routineIds = Object.keys(draft.routines);
  const activeId = activeRoutineId && draft.routines[activeRoutineId] ? activeRoutineId : routineIds[0] ?? null;
  const activeRoutine = activeId ? draft.routines[activeId] : null;

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-white">Routines</h2>
        <p className="text-sm text-content-secondary mt-1">
          Step-by-step routines Jess walks you through. Time + day changes take effect immediately —
          power-user fields like Home Assistant actions are preserved.
        </p>
      </div>

      {/* Routine sub-tabs */}
      {routineIds.length > 1 && (
        <div className="flex flex-wrap gap-2 border-b border-line-subtle pb-2">
          {routineIds.map((rid) => {
            const isActive = rid === activeId;
            return (
              <button
                key={rid}
                type="button"
                onClick={() => setActiveRoutineId(rid)}
                aria-current={isActive ? 'page' : undefined}
                className={`px-3 py-1.5 rounded-md text-sm border transition-colors ${
                  isActive
                    ? 'border-brand-500/60 bg-brand-500/10 text-brand-500'
                    : 'border-line text-content-primary hover:border-line-strong hover:text-white'
                }`}
              >
                {draft.routines[rid].display_name || rid}
              </button>
            );
          })}
        </div>
      )}

      {activeId && activeRoutine && (
        <RoutineEditor
          routineId={activeId}
          routine={activeRoutine}
          onChange={(updater) => patchRoutine(activeId, updater)}
        />
      )}

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

function RoutineEditor({
  routineId,
  routine,
  onChange,
}: {
  routineId: string;
  routine: Routine;
  onChange: (updater: (r: Routine) => Routine) => void;
}) {
  function toggleDay(day: Weekday) {
    onChange((r) => ({
      ...r,
      trigger: {
        ...r.trigger,
        days: r.trigger.days.includes(day)
          ? r.trigger.days.filter((d) => d !== day)
          : [...r.trigger.days, day],
      },
    }));
  }

  function patchStep(idx: number, partial: Partial<RoutineStep>) {
    onChange((r) => {
      const next = [...r.steps];
      next[idx] = { ...next[idx], ...partial };
      return { ...r, steps: next };
    });
  }

  function moveStep(idx: number, dir: -1 | 1) {
    onChange((r) => {
      const next = [...r.steps];
      const target = idx + dir;
      if (target < 0 || target >= next.length) return r;
      [next[idx], next[target]] = [next[target], next[idx]];
      return { ...r, steps: next };
    });
  }

  function removeStep(idx: number) {
    if (routine.steps.length <= 1) return;
    onChange((r) => ({ ...r, steps: r.steps.filter((_, i) => i !== idx) }));
  }

  function addStep() {
    onChange((r) => ({
      ...r,
      steps: [...r.steps, newStep(new Set(r.steps.map((s) => s.id)))],
    }));
  }

  return (
    <div className="space-y-5">
      <div className="grid sm:grid-cols-2 gap-4">
        <label className="flex flex-col gap-1.5">
          <span className="text-xs uppercase tracking-wider text-content-muted">Display name</span>
          <input
            type="text"
            value={routine.display_name}
            onChange={(e) => onChange((r) => ({ ...r, display_name: e.target.value }))}
            maxLength={200}
            className="input"
          />
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-xs uppercase tracking-wider text-content-muted">Speaker (HA entity)</span>
          <input
            type="text"
            value={routine.speaker}
            onChange={(e) => onChange((r) => ({ ...r, speaker: e.target.value }))}
            maxLength={200}
            placeholder="media_player.bedroom_pair"
            className="input font-mono"
          />
        </label>
      </div>

      <div className="grid sm:grid-cols-2 gap-4">
        <label className="flex flex-col gap-1.5">
          <span className="text-xs uppercase tracking-wider text-content-muted">Trigger time</span>
          <input
            type="time"
            value={routine.trigger.time}
            onChange={(e) => onChange((r) => ({ ...r, trigger: { ...r.trigger, time: e.target.value } }))}
            className="input"
          />
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-xs uppercase tracking-wider text-content-muted">Nudge delay (minutes)</span>
          <input
            type="number"
            min={1}
            max={240}
            value={routine.nudge_delay_minutes}
            onChange={(e) => {
              const n = Number(e.target.value);
              if (!Number.isFinite(n)) return;
              onChange((r) => ({ ...r, nudge_delay_minutes: n }));
            }}
            className="input w-32"
          />
        </label>
      </div>

      <div className="grid sm:grid-cols-2 gap-4">
        <label className="flex flex-col gap-1.5">
          <span className="text-xs uppercase tracking-wider text-content-muted">Max nudges per step</span>
          <input
            type="number"
            min={1}
            max={20}
            value={routine.nudge_max}
            onChange={(e) => {
              const n = Number(e.target.value);
              if (!Number.isFinite(n)) return;
              onChange((r) => ({ ...r, nudge_max: Math.max(1, Math.min(20, Math.floor(n))) }));
            }}
            className="input w-32"
          />
          <span className="text-xs text-content-muted">
            After this many &ldquo;still on X?&rdquo; nudges, Jess gives up on the step.
          </span>
        </label>
        <label className="flex items-start gap-3 cursor-pointer pt-6">
          <input
            type="checkbox"
            checked={routine.auto_skip}
            onChange={(e) => onChange((r) => ({ ...r, auto_skip: e.target.checked }))}
            className="mt-1 h-4 w-4 accent-brand-500"
          />
          <span className="flex flex-col">
            <span className="text-sm text-white">Auto-skip on max nudges</span>
            <span className="text-xs text-content-muted">
              When max nudges hit: <strong>on</strong> = advance past skippable steps; <strong>off</strong> = end the whole routine.
            </span>
          </span>
        </label>
      </div>

      <fieldset>
        <legend className="text-xs uppercase tracking-wider text-content-muted mb-2">Active days</legend>
        <div className="flex flex-wrap gap-2">
          {DAY_LABELS.map(({ key, label }) => {
            const active = routine.trigger.days.includes(key);
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

      <div>
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-semibold text-white">Steps ({routine.steps.length})</h3>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={addStep}
          >
            <Plus size={14} />
            Add step
          </Button>
        </div>
        <ul className="space-y-2">
          {routine.steps.map((step, i) => (
            <li
              // Key on step.id only (no index) — otherwise reordering remounts
              // both swapped <input>s and the user loses focus mid-edit.
              // step.id uniqueness within a routine is enforced by both the
              // backend Pydantic regex and `newStep()`'s collision-safe gen.
              key={`${routineId}-${step.id}`}
              className="rounded-lg border border-line bg-surface-base/40 p-3"
            >
              <div className="grid sm:grid-cols-[1fr_120px_auto] gap-3 items-start">
                <label className="flex flex-col gap-1.5 min-w-0">
                  <span className="text-xs uppercase tracking-wider text-content-muted">Label</span>
                  <input
                    type="text"
                    value={step.label}
                    onChange={(e) => patchStep(i, { label: e.target.value })}
                    maxLength={200}
                    className="input"
                  />
                </label>
                <label className="flex flex-col gap-1.5">
                  <span className="text-xs uppercase tracking-wider text-content-muted">Est. min</span>
                  <input
                    type="number"
                    min={0}
                    max={240}
                    value={step.est_minutes}
                    onChange={(e) => {
                      const n = Number(e.target.value);
                      if (!Number.isFinite(n)) return;
                      patchStep(i, { est_minutes: n });
                    }}
                    className="input"
                  />
                </label>
                <div className="flex flex-col gap-1.5">
                  <span className="text-xs uppercase tracking-wider text-content-muted">Actions</span>
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      onClick={() => moveStep(i, -1)}
                      disabled={i === 0}
                      aria-label="Move step up"
                      className="p-1.5 rounded-md border border-line text-content-primary hover:bg-surface-raised disabled:opacity-30 disabled:cursor-not-allowed"
                    >
                      <ArrowUp size={14} />
                    </button>
                    <button
                      type="button"
                      onClick={() => moveStep(i, 1)}
                      disabled={i === routine.steps.length - 1}
                      aria-label="Move step down"
                      className="p-1.5 rounded-md border border-line text-content-primary hover:bg-surface-raised disabled:opacity-30 disabled:cursor-not-allowed"
                    >
                      <ArrowDown size={14} />
                    </button>
                    <button
                      type="button"
                      onClick={() => removeStep(i)}
                      disabled={routine.steps.length <= 1}
                      aria-label="Delete step"
                      className="p-1.5 rounded-md border border-line text-content-primary hover:bg-danger/20 hover:text-danger hover:border-danger/40 disabled:opacity-30 disabled:cursor-not-allowed"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
              </div>
              <label className="mt-3 flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={step.skippable}
                  onChange={(e) => patchStep(i, { skippable: e.target.checked })}
                  className="h-4 w-4 accent-brand-500"
                />
                <span className="text-xs text-content-secondary">
                  Skippable — required for &ldquo;Auto-skip on max nudges&rdquo; above to advance past this step
                </span>
              </label>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
