'use client';

import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { Button, Card } from '@/components/ui';
import { friendlyError } from '@/lib/errors';
import { planApi, type DietProfileOption, type DietSettings, type Weekday } from '@/lib/pantry-api';

const DAYS: { key: Weekday; label: string }[] = [
  { key: 'mon', label: 'Monday' },
  { key: 'tue', label: 'Tuesday' },
  { key: 'wed', label: 'Wednesday' },
  { key: 'thu', label: 'Thursday' },
  { key: 'fri', label: 'Friday' },
  { key: 'sat', label: 'Saturday' },
  { key: 'sun', label: 'Sunday' },
];

function NumberField({
  label,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onChange: (v: number) => void;
}) {
  // Keep the raw text while typing (an empty field is fine mid-edit); clamp
  // only on blur, so clearing "30" to type "45" doesn't turn into "104".
  const [text, setText] = useState(String(value));
  useEffect(() => setText(String(value)), [value]);
  const commit = () => {
    const n = Number(text);
    const clamped = Number.isFinite(n) && text.trim() !== '' ? Math.max(min, Math.min(max, Math.round(n))) : value;
    setText(String(clamped));
    onChange(clamped);
  };
  return (
    <label className="flex items-center justify-between gap-3 text-sm min-h-[40px]">
      <span className="text-content-secondary">{label}</span>
      <input
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        className="input w-20 min-h-[40px] text-base md:text-sm"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
      />
    </label>
  );
}

/** Diet profile + meal-prep schedule, editable in place on the Meal plan tab. */
export default function DietSettingsCard({
  settings,
  profiles,
  onSaved,
}: {
  settings: DietSettings;
  profiles: DietProfileOption[];
  onSaved: (s: DietSettings) => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<DietSettings>(settings);
  const [avoidText, setAvoidText] = useState(settings.avoid.join(', '));
  const [rulesText, setRulesText] = useState(settings.extra_rules.join('\n'));
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // Re-seed from the server only when the user isn't mid-edit.
  const dirty = useRef(false);
  useEffect(() => {
    if (dirty.current) return;
    setDraft(settings);
    setAvoidText(settings.avoid.join(', '));
    setRulesText(settings.extra_rules.join('\n'));
  }, [settings]);

  const profile = profiles.find((p) => p.key === draft.profile);
  const set = <K extends keyof DietSettings>(k: K, v: DietSettings[K]) => {
    dirty.current = true;
    setDraft((d) => ({ ...d, [k]: v }));
  };

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const res = await planApi.saveDiet({
        ...draft,
        avoid: avoidText
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
        extra_rules: rulesText
          .split('\n')
          .map((s) => s.trim())
          .filter(Boolean),
      });
      dirty.current = false;
      onSaved(res.settings);
      setMsg({
        ok: true,
        text: res.rescheduled_plans
          ? 'Saved. Your accepted plan’s reminders moved to the new times.'
          : 'Saved. The next plan will use these.',
      });
    } catch (e) {
      setMsg({ ok: false, text: friendlyError(e, 'Couldn’t save your preferences.', { preferDetail: true }) });
    } finally {
      setSaving(false);
    }
  };

  const summary =
    `${profiles.find((p) => p.key === settings.profile)?.label ?? settings.profile} · ` +
    (settings.batch_lunch_days ? `${settings.batch_lunch_days} prepped lunches · ` : '') +
    `${settings.fresh_dinners_per_week} dinners · for ${settings.servings}`;

  return (
    <Card padding="sm" className="space-y-3">
      <button
        className="w-full flex items-center gap-2 text-left min-h-[40px]"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        <span className="text-sm font-medium text-content-primary">Diet & schedule</span>
        <span className="text-xs text-content-muted truncate min-w-0 flex-1">{summary}</span>
      </button>

      {open && (
        <div className="space-y-4 pt-1">
          <div className="space-y-1">
            <label className="text-sm text-content-secondary" htmlFor="diet-profile">
              Diet
            </label>
            <select
              id="diet-profile"
              className="input w-full text-base md:text-sm"
              value={draft.profile}
              onChange={(e) => set('profile', e.target.value)}
            >
              {profiles.map((p) => (
                <option key={p.key} value={p.key}>
                  {p.label}
                </option>
              ))}
            </select>
            {profile && <p className="text-xs text-content-muted">{profile.description}</p>}
            <p className="text-[11px] text-content-muted">
              Eating preferences, not medical advice. Nutrition numbers on recipes are rough estimates.
            </p>
          </div>

          <div className="space-y-2">
            <label className="flex items-center justify-between gap-3 text-sm">
              <span className="text-content-secondary">Batch-prep day</span>
              <select
                className="input w-auto min-h-[40px] text-base md:text-sm"
                value={draft.batch_day}
                onChange={(e) => set('batch_day', e.target.value as Weekday)}
              >
                {DAYS.map((d) => (
                  <option key={d.key} value={d.key}>
                    {d.label}
                  </option>
                ))}
              </select>
            </label>
            <NumberField
              label="Lunches from the batch (0 = no batch)"
              value={draft.batch_lunch_days}
              min={0}
              max={6}
              onChange={(v) => set('batch_lunch_days', v)}
            />
            <NumberField
              label="Fresh dinners a week"
              value={draft.fresh_dinners_per_week}
              min={0}
              max={7}
              onChange={(v) => set('fresh_dinners_per_week', v)}
            />
            <NumberField
              label="Max dinner cooking time (min)"
              value={draft.max_dinner_minutes}
              min={10}
              max={180}
              onChange={(v) => set('max_dinner_minutes', v)}
            />
            <NumberField label="People" value={draft.servings} min={1} max={8} onChange={(v) => set('servings', v)} />
          </div>

          <div className="space-y-1">
            <label className="text-sm text-content-secondary" htmlFor="diet-avoid">
              Never use (comma-separated)
            </label>
            <input
              id="diet-avoid"
              className="input w-full text-base md:text-sm"
              placeholder="e.g. mushrooms, cilantro"
              value={avoidText}
              onChange={(e) => {
                dirty.current = true;
                setAvoidText(e.target.value);
              }}
            />
          </div>

          <div className="space-y-1">
            <label className="text-sm text-content-secondary" htmlFor="diet-rules">
              Extra wishes (one per line)
            </label>
            <textarea
              id="diet-rules"
              className="input w-full min-h-[72px] text-base md:text-sm"
              placeholder={'e.g. Something spicy once a week\nNo fish on Fridays'}
              value={rulesText}
              onChange={(e) => {
                dirty.current = true;
                setRulesText(e.target.value);
              }}
            />
          </div>

          <fieldset className="space-y-2">
            <legend className="text-sm text-content-secondary mb-1">Cooking reminders (after you accept a plan)</legend>
            <p className="text-[11px] text-content-muted">
              Changing the times also moves the reminders of the plan you’ve already accepted.
            </p>
            <div className="flex items-center justify-between gap-3 text-sm">
              <label className="flex items-center gap-2 min-h-[40px]">
                <input
                  type="checkbox"
                  className="h-5 w-5"
                  checked={draft.reminders.prep}
                  onChange={(e) => set('reminders', { ...draft.reminders, prep: e.target.checked })}
                />
                Batch-prep reminder
              </label>
              <input
                type="time"
                className="input w-auto min-h-[40px] text-base md:text-sm"
                value={draft.reminders.prep_time}
                onChange={(e) => set('reminders', { ...draft.reminders, prep_time: e.target.value })}
                aria-label="Prep reminder time"
              />
            </div>
            <div className="flex items-center justify-between gap-3 text-sm">
              <label className="flex items-center gap-2 min-h-[40px]">
                <input
                  type="checkbox"
                  className="h-5 w-5"
                  checked={draft.reminders.dinners}
                  onChange={(e) => set('reminders', { ...draft.reminders, dinners: e.target.checked })}
                />
                Dinner reminders
              </label>
              <input
                type="time"
                className="input w-auto min-h-[40px] text-base md:text-sm"
                value={draft.reminders.dinner_time}
                onChange={(e) => set('reminders', { ...draft.reminders, dinner_time: e.target.value })}
                aria-label="Dinner reminder time"
              />
            </div>
          </fieldset>

          {msg && (
            <p className={`text-sm ${msg.ok ? 'text-success' : 'text-danger'}`} aria-live="polite">
              {msg.text}
            </p>
          )}
          <Button variant="primary" className="w-full" disabled={saving} onClick={save}>
            {saving ? 'Saving…' : 'Save preferences'}
          </Button>
        </div>
      )}
    </Card>
  );
}
