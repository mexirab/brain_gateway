'use client';

import { useState, useEffect, useCallback } from 'react';
import { Loader2, Plus, Trash2, Edit3, X, Save } from 'lucide-react';
import {
  settingsApi,
  type RecurringRule,
  type RecurringRuleInput,
  type Weekday,
} from '@/lib/settings-api';

const DAY_LABELS: Array<{ key: Weekday; label: string }> = [
  { key: 'mon', label: 'Mon' },
  { key: 'tue', label: 'Tue' },
  { key: 'wed', label: 'Wed' },
  { key: 'thu', label: 'Thu' },
  { key: 'fri', label: 'Fri' },
  { key: 'sat', label: 'Sat' },
  { key: 'sun', label: 'Sun' },
];

const DEFAULT_DRAFT: RecurringRuleInput = {
  text: '',
  cron_expression: '0 9 * * 1-5',
  target: 'both',
  days_of_week: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
  enabled: true,
};

function formatNextFire(iso: string | null): string {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function summarizeDays(daysCsv: string): string {
  const parts = daysCsv.split(',').filter(Boolean);
  if (parts.length === 7) return 'Every day';
  return parts
    .map((d) => DAY_LABELS.find((x) => x.key === (d as Weekday))?.label ?? d)
    .join(', ');
}

import type { DirtyRegister } from '@/app/(private)/settings/page';

interface PanelProps {
  registerDirty?: DirtyRegister;
}

export default function RecurringRemindersPanel({ registerDirty }: PanelProps = {}) {
  const [loading, setLoading] = useState(true);
  const [rules, setRules] = useState<RecurringRule[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState<RecurringRuleInput>(DEFAULT_DRAFT);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState<RecurringRuleInput | null>(null);
  const [working, setWorking] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await settingsApi.listRecurring();
      setRules(data.rules);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load recurring reminders');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // The list itself is never dirty (changes commit immediately on save/delete).
  // Dirty state only exists while the inline add/edit form is open.
  useEffect(() => {
    registerDirty?.('recurring', adding || editingId !== null);
  }, [adding, editingId, registerDirty]);

  async function handleCreate() {
    if (!draft.text.trim()) {
      setError('Reminder text is required');
      return;
    }
    setWorking(true);
    setError(null);
    try {
      await settingsApi.createRecurring(draft);
      setDraft(DEFAULT_DRAFT);
      setAdding(false);
      setStatusMsg('Created.');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Create failed');
    } finally {
      setWorking(false);
    }
  }

  async function handleDelete(id: string) {
    if (!confirm('Delete this recurring reminder?')) return;
    setWorking(true);
    setError(null);
    try {
      await settingsApi.deleteRecurring(id);
      setStatusMsg('Deleted.');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Delete failed');
    } finally {
      setWorking(false);
    }
  }

  async function handleToggle(rule: RecurringRule) {
    setWorking(true);
    setError(null);
    try {
      await settingsApi.updateRecurring(rule.id, { enabled: !rule.enabled });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Toggle failed');
    } finally {
      setWorking(false);
    }
  }

  async function handleSaveEdit() {
    if (!editingId || !editDraft) return;
    setWorking(true);
    setError(null);
    try {
      await settingsApi.updateRecurring(editingId, editDraft);
      setEditingId(null);
      setEditDraft(null);
      setStatusMsg('Updated.');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Update failed');
    } finally {
      setWorking(false);
    }
  }

  function startEdit(rule: RecurringRule) {
    setEditingId(rule.id);
    setEditDraft({
      text: rule.text,
      cron_expression: rule.cron_expression,
      target: rule.target,
      days_of_week: rule.days_of_week.split(',').filter(Boolean) as Weekday[],
      enabled: !!rule.enabled,
    });
    setStatusMsg(null);
    setError(null);
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-10">
        <Loader2 className="animate-spin text-brand-500" size={24} />
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-white">Recurring Reminders</h2>
          <p className="text-sm text-zinc-400 mt-1">
            Schedule reminders that fire on a repeating cadence. Cron syntax: <code className="bg-zinc-800 px-1 rounded">m h dom mon dow</code>.
          </p>
        </div>
        {!adding && (
          <button
            type="button"
            onClick={() => {
              setAdding(true);
              setDraft(DEFAULT_DRAFT);
              setError(null);
              setStatusMsg(null);
            }}
            className="flex items-center gap-1.5 px-3 py-2 bg-brand-600 hover:bg-brand-700 text-white text-sm rounded-md transition-colors"
          >
            <Plus size={14} />
            Add rule
          </button>
        )}
      </div>

      {(error || statusMsg) && (
        <div
          className={`text-sm rounded-md px-3 py-2 ${
            error ? 'bg-red-500/10 border border-red-500/30 text-red-300' : 'bg-emerald-500/10 border border-emerald-500/30 text-emerald-300'
          }`}
        >
          {error ?? statusMsg}
        </div>
      )}

      {adding && (
        <RuleForm
          draft={draft}
          onChange={setDraft}
          onSubmit={handleCreate}
          onCancel={() => {
            setAdding(false);
            setDraft(DEFAULT_DRAFT);
          }}
          submitLabel="Create"
          working={working}
        />
      )}

      {rules.length === 0 && !adding ? (
        <p className="text-sm text-zinc-500 italic">No recurring reminders yet.</p>
      ) : (
        <ul className="space-y-2">
          {rules.map((rule) => (
            <li
              key={rule.id}
              className={`rounded-lg border p-4 ${rule.enabled ? 'border-zinc-700 bg-zinc-900/40' : 'border-zinc-800 bg-zinc-900/20 opacity-70'}`}
            >
              {editingId === rule.id && editDraft ? (
                <RuleForm
                  draft={editDraft}
                  onChange={setEditDraft}
                  onSubmit={handleSaveEdit}
                  onCancel={() => {
                    setEditingId(null);
                    setEditDraft(null);
                  }}
                  submitLabel="Save"
                  working={working}
                />
              ) : (
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm text-white truncate">{rule.text}</p>
                    <p className="text-xs text-zinc-500 mt-1">
                      <code className="bg-zinc-800 px-1 rounded">{rule.cron_expression}</code> · {summarizeDays(rule.days_of_week)} · {rule.target}
                    </p>
                    <p className="text-xs text-zinc-500 mt-1">
                      Next fire: {formatNextFire(rule.next_fire_at)}
                    </p>
                  </div>
                  <div className="flex items-center gap-1.5 flex-shrink-0">
                    <label className="flex items-center gap-1 cursor-pointer text-xs text-zinc-400">
                      <input
                        type="checkbox"
                        checked={!!rule.enabled}
                        onChange={() => handleToggle(rule)}
                        disabled={working}
                        className="h-4 w-4 accent-brand-500"
                      />
                      On
                    </label>
                    <button
                      type="button"
                      onClick={() => startEdit(rule)}
                      disabled={working}
                      aria-label="Edit"
                      className="p-1.5 rounded-md border border-zinc-700 text-zinc-300 hover:bg-zinc-800"
                    >
                      <Edit3 size={14} />
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDelete(rule.id)}
                      disabled={working}
                      aria-label="Delete"
                      className="p-1.5 rounded-md border border-zinc-700 text-zinc-300 hover:bg-red-500/20 hover:text-red-300 hover:border-red-500/40"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function RuleForm({
  draft,
  onChange,
  onSubmit,
  onCancel,
  submitLabel,
  working,
}: {
  draft: RecurringRuleInput;
  onChange: (next: RecurringRuleInput) => void;
  onSubmit: () => void;
  onCancel: () => void;
  submitLabel: string;
  working: boolean;
}) {
  function toggleDay(day: Weekday) {
    const days = draft.days_of_week ?? [];
    onChange({
      ...draft,
      days_of_week: days.includes(day) ? days.filter((d) => d !== day) : [...days, day],
    });
  }

  return (
    <div className="space-y-3">
      <label className="flex flex-col gap-1.5">
        <span className="text-xs uppercase tracking-wider text-zinc-500">Reminder text</span>
        <input
          type="text"
          value={draft.text}
          onChange={(e) => onChange({ ...draft, text: e.target.value })}
          maxLength={500}
          placeholder="e.g. Take vitamins"
          className="bg-zinc-900 border border-zinc-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
        />
      </label>
      <div className="grid sm:grid-cols-2 gap-3">
        <label className="flex flex-col gap-1.5">
          <span className="text-xs uppercase tracking-wider text-zinc-500">Cron expression</span>
          <input
            type="text"
            value={draft.cron_expression}
            onChange={(e) => onChange({ ...draft, cron_expression: e.target.value })}
            placeholder="0 9 * * 1-5"
            className="bg-zinc-900 border border-zinc-700 rounded-md px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-brand-500"
          />
          <span className="text-xs text-zinc-500">
            Examples: <code>0 9 * * *</code> (9am daily), <code>0 9 * * 1-5</code> (weekdays 9am)
          </span>
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-xs uppercase tracking-wider text-zinc-500">Channel</span>
          <select
            value={draft.target ?? 'both'}
            onChange={(e) =>
              onChange({ ...draft, target: e.target.value as 'tts' | 'push' | 'both' })
            }
            className="bg-zinc-900 border border-zinc-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
          >
            <option value="both">TTS + Push</option>
            <option value="tts">TTS only</option>
            <option value="push">Push only</option>
          </select>
        </label>
      </div>
      <fieldset>
        <legend className="text-xs uppercase tracking-wider text-zinc-500 mb-2">Day filter</legend>
        <div className="flex flex-wrap gap-2">
          {DAY_LABELS.map(({ key, label }) => {
            const active = (draft.days_of_week ?? []).includes(key);
            return (
              <button
                key={key}
                type="button"
                onClick={() => toggleDay(key)}
                aria-pressed={active}
                className={`px-3 py-1 rounded-full text-sm border transition-colors ${
                  active
                    ? 'border-brand-500/60 bg-brand-500/15 text-brand-500'
                    : 'border-zinc-700 text-zinc-400 hover:border-zinc-500 hover:text-white'
                }`}
              >
                {label}
              </button>
            );
          })}
        </div>
      </fieldset>
      <div className="flex items-center gap-2 pt-2">
        <button
          type="button"
          onClick={onSubmit}
          disabled={working}
          className="flex items-center gap-1.5 px-3 py-2 rounded-md bg-brand-600 hover:bg-brand-700 disabled:opacity-40 text-white text-sm transition-colors"
        >
          {working ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
          {submitLabel}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={working}
          className="flex items-center gap-1.5 px-3 py-2 rounded-md border border-zinc-700 text-zinc-300 hover:bg-zinc-800 disabled:opacity-40 text-sm transition-colors"
        >
          <X size={14} />
          Cancel
        </button>
      </div>
    </div>
  );
}
