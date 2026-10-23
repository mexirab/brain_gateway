'use client';

import { useState, useEffect, useCallback } from 'react';
import { Loader2, Plus, Trash2, Save, X, Check } from 'lucide-react';
import { settingsApi, SettingsApiError, type LocationTemplate } from '@/lib/settings-api';
import type { WorkoutGoal, WorkoutLocation, WorkoutProgram } from '@/lib/types';
import { Button } from '@/components/ui';
import type { DirtyRegister } from '@/app/(private)/settings/page';
import { friendlyError } from '@/lib/errors';

const EQUIPMENT_LABELS: Record<string, string> = {
  barbell: 'Barbell',
  squat_rack: 'Squat rack',
  bench: 'Bench',
  trap_bar: 'Trap bar',
  dumbbells: 'Dumbbells',
  kettlebell: 'Kettlebell',
  cable: 'Cable station',
  machines: 'Machines',
  leg_press: 'Leg press',
  pull_up_bar: 'Pull-up bar',
  dip_station: 'Dip station',
  ab_wheel: 'Ab wheel',
};

const GOAL_LABELS: Record<WorkoutGoal, string> = {
  general: 'General fitness',
  strength: 'Strength (heavier, fewer reps)',
  muscle: 'Muscle (moderate weight, more reps)',
};

const TEMPLATE_LABELS: Record<LocationTemplate, string> = {
  commercial_gym: 'Full commercial gym',
  home_basic: 'Home: dumbbells + kettlebell',
  empty: 'Nothing (bodyweight only)',
};

interface ProgramDraft {
  goal: WorkoutGoal;
  days_per_week: number;
  session_minutes: number;
  deload_every_weeks: number;
}

const DEFAULT_PROGRAM: ProgramDraft = { goal: 'general', days_per_week: 3, session_minutes: 45, deload_every_weeks: 6 };

const DELOAD_OPTIONS = [0, 4, 5, 6, 8];

const MINUTE_OPTIONS = [20, 30, 45, 60, 75, 90, 120];

function programToDraft(p: WorkoutProgram | null): ProgramDraft {
  return p
    ? {
        goal: p.goal,
        days_per_week: p.days_per_week,
        session_minutes: p.session_minutes,
        deload_every_weeks: p.deload_every_weeks ?? DEFAULT_PROGRAM.deload_every_weeks,
      }
    : DEFAULT_PROGRAM;
}

interface LocationDraft {
  equipment: string[];
  dumbbell_max_lbs: string;
  dumbbell_step_lbs: string;
  kettlebell_weights_lbs: string;
}

function toDraft(loc: WorkoutLocation): LocationDraft {
  return {
    equipment: [...loc.equipment],
    dumbbell_max_lbs: loc.limits.dumbbell_max_lbs?.toString() ?? '',
    dumbbell_step_lbs: loc.limits.dumbbell_step_lbs?.toString() ?? '',
    kettlebell_weights_lbs: (loc.limits.kettlebell_weights_lbs ?? []).join(', '),
  };
}

function sameDraft(a: LocationDraft, b: LocationDraft): boolean {
  return (
    a.equipment.slice().sort().join() === b.equipment.slice().sort().join() &&
    a.dumbbell_max_lbs === b.dumbbell_max_lbs &&
    a.dumbbell_step_lbs === b.dumbbell_step_lbs &&
    a.kettlebell_weights_lbs.replace(/\s/g, '') === b.kettlebell_weights_lbs.replace(/\s/g, '')
  );
}

/** '' → null (clears the limit), otherwise a number (NaN is rejected by the backend). */
function numOrNull(raw: string): number | null {
  return raw.trim() === '' ? null : Number(raw);
}

interface PanelProps {
  registerDirty?: DirtyRegister;
}

export default function WorkoutsPanel({ registerDirty }: PanelProps = {}) {
  const [loading, setLoading] = useState(true);
  const [disabled, setDisabled] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [working, setWorking] = useState(false);

  const [program, setProgram] = useState<WorkoutProgram | null>(null);
  const [programDraft, setProgramDraft] = useState<ProgramDraft>(DEFAULT_PROGRAM);

  const [locations, setLocations] = useState<WorkoutLocation[]>([]);
  const [vocabulary, setVocabulary] = useState<string[]>(Object.keys(EQUIPMENT_LABELS));
  const [drafts, setDrafts] = useState<Record<number, LocationDraft>>({});
  const [newName, setNewName] = useState('');
  const [newTemplate, setNewTemplate] = useState<LocationTemplate>('commercial_gym');

  // `refresh` re-reads server state after a save WITHOUT the full-panel
  // spinner, keeping unsaved edits elsewhere on the panel: drafts are only
  // replaced for `resetIds`, and the program draft only when `resetProgram`.
  const load = useCallback(
    async (refresh?: { resetIds: number[]; resetProgram: boolean }) => {
      if (!refresh) setLoading(true);
      setError(null);
      try {
        const [prog, locs] = await Promise.all([
          settingsApi.getWorkoutProgram(),
          settingsApi.listWorkoutLocations(),
        ]);
        setProgram(prog.program);
        if (!refresh || refresh.resetProgram) setProgramDraft(programToDraft(prog.program));
        setLocations(locs.locations);
        setVocabulary(locs.equipment_vocabulary);
        setDrafts((prev) =>
          Object.fromEntries(
            locs.locations.map((l) => [
              l.id,
              refresh && prev[l.id] && !refresh.resetIds.includes(l.id) ? prev[l.id] : toDraft(l),
            ]),
          ),
        );
      } catch (e) {
        // Workout routes aren't mounted when the feature is off.
        if (e instanceof SettingsApiError && e.status === 404) setDisabled(true);
        else setError(friendlyError(e, 'Couldn’t load workout settings.'));
      } finally {
        if (!refresh) setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    load();
  }, [load]);

  const programBaseline = programToDraft(program);
  const programDirty =
    programBaseline.goal !== programDraft.goal ||
    programBaseline.days_per_week !== programDraft.days_per_week ||
    programBaseline.session_minutes !== programDraft.session_minutes ||
    programBaseline.deload_every_weeks !== programDraft.deload_every_weeks;
  const dirtyLocationIds = locations
    .filter((l) => drafts[l.id] && !sameDraft(drafts[l.id], toDraft(l)))
    .map((l) => l.id);

  useEffect(() => {
    registerDirty?.(
      'workouts',
      programDirty || dirtyLocationIds.length > 0 || newName.trim() !== '',
    );
  }, [programDirty, dirtyLocationIds.length, newName, registerDirty]);

  async function run(
    action: () => Promise<unknown>,
    success: string,
    failure: string,
    reset: { resetIds: number[]; resetProgram: boolean } = { resetIds: [], resetProgram: false },
  ) {
    setWorking(true);
    setError(null);
    setStatusMsg(null);
    try {
      await action();
      setStatusMsg(success);
      await load(reset);
    } catch (e) {
      setError(friendlyError(e, failure, { preferDetail: true }));
    } finally {
      setWorking(false);
    }
  }

  function saveProgram() {
    return run(() => settingsApi.setWorkoutProgram(programDraft), 'Program saved.', 'Couldn’t save the program.', {
      resetIds: [],
      resetProgram: true,
    });
  }

  function clearProgram() {
    if (!confirm('Turn off the weekly program? Jess goes back to planning day by day.')) return;
    return run(() => settingsApi.clearWorkoutProgram(), 'Program turned off.', 'Couldn’t turn off the program.', {
      resetIds: [],
      resetProgram: true,
    });
  }

  function addLocation() {
    const name = newName.trim();
    if (!name) {
      setError('Give the location a name.');
      return;
    }
    return run(
      async () => {
        await settingsApi.createWorkoutLocation(name, newTemplate);
        setNewName('');
      },
      `Added ${name}.`,
      'Couldn’t add that location.',
    );
  }

  function saveLocation(loc: WorkoutLocation) {
    const d = drafts[loc.id];
    const hasDumbbells = d.equipment.includes('dumbbells');
    const hasKettlebell = d.equipment.includes('kettlebell');
    const bells = d.kettlebell_weights_lbs
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
      .map(Number);
    return run(
      () =>
        settingsApi.updateWorkoutLocation(loc.id, {
          equipment: d.equipment,
          // Limits for gear that's toggled off are cleared, not sent hidden.
          limits: {
            dumbbell_max_lbs: hasDumbbells ? numOrNull(d.dumbbell_max_lbs) : null,
            dumbbell_step_lbs: hasDumbbells ? numOrNull(d.dumbbell_step_lbs) : null,
            kettlebell_weights_lbs: hasKettlebell && bells.length ? bells : null,
          },
        }),
      `Saved ${loc.name}.`,
      `Couldn’t save ${loc.name}.`,
      { resetIds: [loc.id], resetProgram: false },
    );
  }

  function deleteLocation(loc: WorkoutLocation) {
    if (!confirm(`Delete ${loc.name}? Past workouts there are kept.`)) return;
    return run(() => settingsApi.deleteWorkoutLocation(loc.id), `Deleted ${loc.name}.`, `Couldn’t delete ${loc.name}.`, {
      resetIds: [loc.id],
      resetProgram: false,
    });
  }

  function updateDraft(id: number, change: Partial<LocationDraft>) {
    setDrafts((prev) => ({ ...prev, [id]: { ...prev[id], ...change } }));
  }

  function toggleEquipment(id: number, token: string) {
    const current = drafts[id]?.equipment ?? [];
    updateDraft(id, {
      equipment: current.includes(token) ? current.filter((t) => t !== token) : [...current, token],
    });
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-10">
        <Loader2 className="animate-spin text-brand-500" size={24} />
      </div>
    );
  }

  if (disabled) {
    return (
      <div className="space-y-2">
        <h2 className="text-lg font-semibold text-content-primary">Workouts</h2>
        <p className="text-sm text-content-secondary">Workout tracking isn’t enabled on this install.</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-content-primary">Workouts</h2>
        <p className="text-sm text-content-secondary mt-1">
          Your weekly program, and what equipment each place you train has. Jess only plans
          exercises the place you’re at can support.
        </p>
      </div>

      {(error || statusMsg) && (
        <div
          role={error ? 'alert' : 'status'}
          className={`text-sm rounded-md px-3 py-2 ${
            error ? 'bg-danger/10 border border-danger/30 text-danger' : 'bg-success/10 border border-success/30 text-success'
          }`}
        >
          {error ?? statusMsg}
        </div>
      )}

      {/* Program */}
      <section className="space-y-3">
        <h3 className="text-sm font-semibold text-content-primary">Weekly program</h3>
        {program ? (
          <p className="text-sm text-content-secondary">
            {program.split.join(' / ')} · next up: <span className="text-content-primary">{program.next_day_label}</span> ·
            about {program.exercises_per_session} exercises per session
            {program.deload?.is_deload
              ? ' · this week is a deload week'
              : program.deload?.sessions_until_deload != null &&
                ` · deload week in ${program.deload.sessions_until_deload} session${
                  program.deload.sessions_until_deload === 1 ? '' : 's'
                }`}
          </p>
        ) : (
          <p className="text-sm text-content-muted italic">
            No program yet. Jess plans each workout from what you trained recently.
          </p>
        )}
        <div className="grid sm:grid-cols-2 gap-3">
          <label className="flex flex-col gap-1.5">
            <span className="text-xs uppercase tracking-wider text-content-muted">Goal</span>
            <select
              value={programDraft.goal}
              onChange={(e) => setProgramDraft({ ...programDraft, goal: e.target.value as WorkoutGoal })}
              className="input"
            >
              {(Object.keys(GOAL_LABELS) as WorkoutGoal[]).map((g) => (
                <option key={g} value={g}>
                  {GOAL_LABELS[g]}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-xs uppercase tracking-wider text-content-muted">Days per week</span>
            <select
              value={programDraft.days_per_week}
              onChange={(e) => setProgramDraft({ ...programDraft, days_per_week: Number(e.target.value) })}
              className="input"
            >
              {[2, 3, 4, 5, 6].map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-xs uppercase tracking-wider text-content-muted">Minutes per session</span>
            <select
              value={programDraft.session_minutes}
              onChange={(e) => setProgramDraft({ ...programDraft, session_minutes: Number(e.target.value) })}
              className="input"
            >
              {/* Keep a value set by chat (e.g. 50) selectable. */}
              {Array.from(new Set([...MINUTE_OPTIONS, programDraft.session_minutes]))
                .sort((a, b) => a - b)
                .map((m) => (
                  <option key={m} value={m}>
                    {m} min
                  </option>
                ))}
            </select>
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-xs uppercase tracking-wider text-content-muted">Lighter deload week</span>
            <select
              value={programDraft.deload_every_weeks}
              onChange={(e) => setProgramDraft({ ...programDraft, deload_every_weeks: Number(e.target.value) })}
              className="input"
            >
              {Array.from(new Set([...DELOAD_OPTIONS, programDraft.deload_every_weeks]))
                .sort((a, b) => a - b)
                .map((w) => (
                  <option key={w} value={w}>
                    {w === 0 ? 'Never' : `After every ${w} weeks of training`}
                  </option>
                ))}
            </select>
          </label>
        </div>
        <p className="text-xs text-content-muted">
          Weights go up when you finish every set, hold when you miss, and drop ~10% after two misses in a row.
          A deload week is lighter on purpose so you recover; it counts weeks of planned sessions you actually
          trained, not calendar weeks. Exercises stay the same until the next deload, then rotate.
        </p>
        <div className="flex items-center gap-2">
          <Button type="button" variant="primary" onClick={saveProgram} disabled={working || (!!program && !programDirty)}>
            {working ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
            {program ? 'Save program' : 'Start program'}
          </Button>
          {program && (
            <Button type="button" variant="secondary" onClick={clearProgram} disabled={working}>
              <X size={14} />
              Turn off
            </Button>
          )}
        </div>
      </section>

      {/* Locations */}
      <section className="space-y-3">
        <h3 className="text-sm font-semibold text-content-primary">Where you train</h3>
        {locations.length === 0 && (
          <p className="text-sm text-content-muted italic">
            No locations yet. Without one, Jess assumes a full gym.
          </p>
        )}
        <ul className="space-y-3">
          {locations.map((loc) => {
            const d = drafts[loc.id];
            if (!d) return null;
            const dirty = dirtyLocationIds.includes(loc.id);
            return (
              <li key={loc.id} className="rounded-lg border border-line bg-surface-base/40 p-4 space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <p className="text-sm font-medium text-content-primary truncate">{loc.name}</p>
                  <button
                    type="button"
                    onClick={() => deleteLocation(loc)}
                    disabled={working}
                    aria-label={`Delete ${loc.name}`}
                    className="p-2.5 rounded-md border border-line text-content-primary hover:bg-danger/20 hover:text-danger hover:border-danger/40 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
                <fieldset>
                  <legend className="text-xs uppercase tracking-wider text-content-muted mb-2">Equipment</legend>
                  <div className="flex flex-wrap gap-2">
                    {vocabulary.map((token) => {
                      const on = d.equipment.includes(token);
                      return (
                        <button
                          key={token}
                          type="button"
                          onClick={() => toggleEquipment(loc.id, token)}
                          aria-pressed={on}
                          className={`inline-flex items-center gap-1.5 px-3 py-2 min-h-[40px] rounded-full text-sm border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 ${
                            on
                              ? 'border-brand-500/60 bg-brand-500/15 text-brand-500'
                              : 'border-line text-content-secondary hover:border-line-strong hover:text-content-primary'
                          }`}
                        >
                          {on && <Check size={12} aria-hidden="true" />}
                          {EQUIPMENT_LABELS[token] ?? token}
                        </button>
                      );
                    })}
                  </div>
                </fieldset>
                {(d.equipment.includes('dumbbells') || d.equipment.includes('kettlebell')) && (
                  <div className="grid sm:grid-cols-3 gap-3">
                    {d.equipment.includes('dumbbells') && (
                      <>
                        <label className="flex flex-col gap-1.5">
                          <span className="text-xs uppercase tracking-wider text-content-muted">Heaviest dumbbell (lb)</span>
                          <input
                            type="number"
                            min={1}
                            max={300}
                            step={0.5}
                            value={d.dumbbell_max_lbs}
                            onChange={(e) => updateDraft(loc.id, { dumbbell_max_lbs: e.target.value })}
                            placeholder="no limit"
                            className="input"
                          />
                        </label>
                        <label className="flex flex-col gap-1.5">
                          <span className="text-xs uppercase tracking-wider text-content-muted">Dumbbell step (lb)</span>
                          <input
                            type="number"
                            min={0.5}
                            max={50}
                            step={0.5}
                            value={d.dumbbell_step_lbs}
                            onChange={(e) => updateDraft(loc.id, { dumbbell_step_lbs: e.target.value })}
                            placeholder="e.g. 2.5"
                            className="input"
                          />
                        </label>
                      </>
                    )}
                    {d.equipment.includes('kettlebell') && (
                      <label className="flex flex-col gap-1.5">
                        <span className="text-xs uppercase tracking-wider text-content-muted">Kettlebells (lb)</span>
                        <input
                          type="text"
                          inputMode="decimal"
                          value={d.kettlebell_weights_lbs}
                          onChange={(e) => updateDraft(loc.id, { kettlebell_weights_lbs: e.target.value })}
                          placeholder="e.g. 35, 53"
                          className="input"
                        />
                      </label>
                    )}
                  </div>
                )}
                {dirty && (
                  <div className="flex items-center gap-2">
                    <Button type="button" variant="primary" onClick={() => saveLocation(loc)} disabled={working}>
                      {working ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                      Save
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={() => updateDraft(loc.id, toDraft(loc))}
                      disabled={working}
                    >
                      <X size={14} />
                      Undo
                    </Button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>

        <form
          className="rounded-lg border border-dashed border-line p-4 space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            addLocation();
          }}
        >
          <div className="grid sm:grid-cols-2 gap-3">
            <label className="flex flex-col gap-1.5">
              <span className="text-xs uppercase tracking-wider text-content-muted">New location</span>
              <input
                type="text"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                maxLength={60}
                placeholder="e.g. Home, or your gym's name"
                className="input"
              />
            </label>
            <label className="flex flex-col gap-1.5">
              <span className="text-xs uppercase tracking-wider text-content-muted">Start from</span>
              <select
                value={newTemplate}
                onChange={(e) => setNewTemplate(e.target.value as LocationTemplate)}
                className="input"
              >
                {(Object.keys(TEMPLATE_LABELS) as LocationTemplate[]).map((t) => (
                  <option key={t} value={t}>
                    {TEMPLATE_LABELS[t]}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <Button type="submit" variant="primary" disabled={working}>
            <Plus size={14} />
            Add location
          </Button>
        </form>
      </section>
    </div>
  );
}
