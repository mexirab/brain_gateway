'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  Check,
  ChefHat,
  ChevronDown,
  ChevronRight,
  Loader2,
  RefreshCw,
  Shuffle,
  ThumbsDown,
  ThumbsUp,
  X,
} from 'lucide-react';
import { Badge, Button, Card, ErrorState } from '@/components/ui';
import { friendlyError } from '@/lib/errors';
import {
  PantryApiError,
  planApi,
  type DietProfileOption,
  type DietSettings,
  type CookResult,
  type MealPlan,
  type PlanEntry,
  type Recipe,
} from '@/lib/pantry-api';
import CookedOtherCard from './CookedOtherCard';
import DietSettingsCard from './DietSettingsCard';

const POLL_MS = 4000;
const MAX_POLL_MS = 30000;
// Matches the backend's STUCK_PLAN_MINUTES: after this a generating plan can be discarded.
const STUCK_MS = 10 * 60 * 1000;
const WEEKDAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

function dayLabel(iso: string): string {
  const d = new Date(`${iso}T12:00:00`);
  return `${WEEKDAY[d.getDay()]} ${d.getDate()}`;
}

/** Like friendlyError, but also shows the backend's reason on 5xx when it
 *  gave one ("Helios may be asleep") — that's actionable, not internal. */
function planError(e: unknown, fallback: string): string {
  if (e instanceof PantryApiError && e.detail) return e.detail;
  return friendlyError(e, fallback);
}

export default function PlanTab({ shown }: { shown: boolean }) {
  const [active, setActive] = useState<MealPlan | null>(null);
  const [draft, setDraft] = useState<MealPlan | null>(null);
  const [settings, setSettings] = useState<DietSettings | null>(null);
  const [profiles, setProfiles] = useState<DietProfileOption[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [pollHiccup, setPollHiccup] = useState(false);
  const [busy, setBusy] = useState(false);
  // One swap at a time per page: concurrent swaps race on the plan's reminders.
  const [swapping, setSwapping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // The cook summary lands at the top; on a phone the button was far below.
  const noticeRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (notice) noticeRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [notice]);
  const [status, setStatus] = useState('');
  // "Need to buy" unticks per draft id — survive reloads and tab switches.
  const [buyByPlan, setBuyByPlan] = useState<Record<number, Record<string, boolean>>>({});

  // Plans are reloaded often (poll, actions); diet settings only once and on
  // save, so an open settings form is never reset under the user's fingers.
  const loadPlans = useCallback(async () => {
    const plans = await planApi.current();
    setActive(plans.active);
    setDraft(plans.draft);
    return plans;
  }, []);

  const loadAll = useCallback(async () => {
    try {
      await loadPlans();
      if (!settings) {
        const diet = await planApi.diet();
        setSettings(diet.settings);
        setProfiles(diet.profiles);
      }
      setLoadFailed(false);
    } catch (e) {
      setLoadFailed(true);
      setError(friendlyError(e, 'Couldn’t load the meal plan.'));
    } finally {
      setLoaded(true);
    }
  }, [loadPlans, settings]);

  useEffect(() => {
    if (shown) loadAll();
    // loadAll changes identity when settings load; only react to visibility.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown]);

  // Poll while a plan is being made: setTimeout chain with backoff, so one
  // wifi drop neither stacks requests nor flashes an error banner.
  const generating = draft?.status === 'generating';
  const lastDraftStatus = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!generating || !shown) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let delay = POLL_MS;
    const tick = async () => {
      try {
        const plans = await loadPlans();
        if (cancelled) return;
        setPollHiccup(false);
        delay = POLL_MS;
        if (plans.draft?.status !== 'generating') return;
      } catch {
        if (cancelled) return;
        setPollHiccup(true);
        delay = Math.min(delay * 2, MAX_POLL_MS);
      }
      timer = setTimeout(tick, delay);
    };
    timer = setTimeout(tick, delay);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [generating, shown, loadPlans]);

  // Screen-reader status for state changes that happen off-screen.
  useEffect(() => {
    const s = draft?.status;
    if (s === lastDraftStatus.current) return;
    lastDraftStatus.current = s;
    if (s === 'generating') setStatus('Planning your week…');
    else if (s === 'draft') setStatus('Your draft plan is ready.');
    else if (s === 'failed') setStatus('Couldn’t make a plan.');
  }, [draft?.status]);

  const run = async (fn: () => Promise<unknown>, after?: (r: unknown) => void) => {
    setBusy(true);
    setError(null);
    try {
      after?.(await fn());
    } catch (e) {
      setError(planError(e, 'That didn’t work — try again.'));
    } finally {
      // Reload on failure too: a 409 usually means the plan changed elsewhere.
      try {
        await loadPlans();
      } catch {
        /* the error banner already explains */
      }
      setBusy(false);
    }
  };

  const generate = () =>
    run(
      () => planApi.generate(),
      () => setNotice(null),
    );

  const redo = () => {
    if (window.confirm('Throw away this draft and make a new plan?')) generate();
  };

  const discard = (plan: MealPlan) => {
    if (window.confirm('Throw away this draft plan?')) run(() => planApi.discard(plan.id));
  };

  // Consumption tracking (phase 4): only on the accepted plan.
  const track: Tracker = {
    cooked: (entryId) =>
      run(
        () => planApi.cooked(entryId),
        (r) => {
          const res = r as CookResult;
          const bits = [`Marked ${res.recipe} cooked.`];
          if (res.deducted.length) bits.push(`Used: ${res.deducted.join(', ')}.`);
          if (res.portions) bits.push(`${res.portions} portion${res.portions === 1 ? '' : 's'} in the fridge.`);
          if (res.running_low.length) bits.push(`Running low: ${res.running_low.join(', ')}.`);
          if (res.not_tracked.length) {
            const more = res.not_tracked.length > 5 ? ` +${res.not_tracked.length - 5} more` : '';
            bits.push(`Not tracked: ${res.not_tracked.slice(0, 5).join(', ')}${more}.`);
          }
          setNotice(bits.join(' '));
        },
      ),
    uncook: (entryId) =>
      run(
        () => planApi.uncook(entryId),
        () => setNotice('Undone — everything is back in the pantry.'),
      ),
    skip: (entryId) =>
      run(
        () => planApi.skip(entryId),
        () => setNotice('Skipped — the ingredients stay in the pantry.'),
      ),
    eat: (batchId) =>
      run(
        () => planApi.eat(batchId),
        (r) => setNotice(`Enjoy! ${(r as { portions_left: number }).portions_left} left.`),
      ),
  };

  const swap = async (entryId: number) => {
    setSwapping(true);
    setError(null);
    try {
      const res = await planApi.swap(entryId);
      if (res.added_to_list) {
        setNotice(
          `Swapped. Added ${res.added_to_list} new item${res.added_to_list === 1 ? '' : 's'} to your grocery list.`,
        );
      }
      await loadPlans();
    } catch (e) {
      setError(planError(e, 'Couldn’t swap that meal — try again.'));
    } finally {
      setSwapping(false);
    }
  };

  if (!loaded) return <Card padding="sm" className="h-24 animate-pulse" />;

  if (loadFailed && !active && !draft && !settings) {
    return (
      <ErrorState
        title="Couldn’t load the meal plan"
        message="Check your connection and try again."
        onRetry={() => {
          setError(null);
          loadAll();
        }}
      />
    );
  }

  const sameWeek = !!(draft && active && draft.week_start === active.week_start);

  return (
    <div className="space-y-5">
      <p className="sr-only" aria-live="polite">
        {status}
      </p>
      {error && (
        <Card
          padding="none"
          role="alert"
          className="p-3 text-sm text-danger/80 border-danger/30 flex items-start gap-2"
        >
          <span className="flex-1">{error}</span>
          <Button icon size="sm" variant="ghost" onClick={() => setError(null)} aria-label="Dismiss error">
            <X size={16} />
          </Button>
        </Card>
      )}
      <div ref={noticeRef} />
      {notice && (
        <Card
          padding="none"
          role="status"
          className="p-3 text-sm text-success border-success/30 flex items-start gap-2"
        >
          <span className="flex-1">{notice}</span>
          <Button icon size="sm" variant="ghost" onClick={() => setNotice(null)} aria-label="Dismiss">
            <X size={16} />
          </Button>
        </Card>
      )}

      {settings && <DietSettingsCard settings={settings} profiles={profiles} onSaved={(s) => setSettings(s)} />}

      {draft?.status === 'generating' && (
        <GeneratingCard
          plan={draft}
          hiccup={pollHiccup}
          busy={busy}
          onDiscard={() => run(() => planApi.discard(draft.id))}
        />
      )}
      {draft?.status === 'failed' && (
        <Card padding="sm" className="space-y-3 border-danger/30">
          <div className="text-sm text-danger">Couldn’t make a plan.</div>
          {draft.error && <div className="text-xs text-content-muted">{draft.error}</div>}
          <Button variant="primary" disabled={busy} onClick={generate}>
            <RefreshCw size={14} /> Try again
          </Button>
        </Card>
      )}
      {draft?.status === 'draft' && (
        <DraftPlan
          plan={draft}
          busy={busy || swapping}
          swapping={swapping}
          sameWeekAsActive={sameWeek}
          buy={buyByPlan[draft.id] ?? {}}
          setBuy={(item, value) =>
            setBuyByPlan((all) => ({ ...all, [draft.id]: { ...(all[draft.id] ?? {}), [item]: value } }))
          }
          onAccept={(have) =>
            run(
              () => planApi.accept(draft.id, have),
              (r) => {
                const res = r as { added_to_list: number; added_to_pantry: number; reminders: number };
                const parts = [
                  `${res.added_to_list} new item${res.added_to_list === 1 ? '' : 's'} on your grocery list`,
                ];
                if (res.added_to_pantry) parts.push(`${res.added_to_pantry} marked as on hand`);
                if (res.reminders) parts.push(`${res.reminders} cooking reminder${res.reminders === 1 ? '' : 's'} set`);
                setNotice(`Plan accepted: ${parts.join(', ')}.`);
              },
            )
          }
          onRedo={redo}
          onDiscard={() => discard(draft)}
          onSwap={swap}
        />
      )}

      {active && (
        <ActivePlan
          plan={active}
          collapsedByDefault={draft?.status === 'draft'}
          busy={busy || swapping}
          swapping={swapping}
          showNewPlan={!draft}
          onNewPlan={generate}
          onSwap={swap}
          track={track}
        />
      )}

      {!active && !draft && (
        <Card padding="sm" className="space-y-3 text-center">
          <ChefHat size={36} className="mx-auto text-content-muted opacity-50" />
          <p className="text-sm text-content-secondary">
            Jess plans a batch prep and a few quick dinners from what you have, then lists what to buy.
          </p>
          <Button variant="primary" className="w-full" disabled={busy} onClick={generate}>
            Plan my week
          </Button>
        </Card>
      )}

      <CookedOtherCard
        shown={shown}
        onChanged={() => {
          // Pantry amounts changed: refresh the plan's "have" badges.
          loadPlans().catch(() => undefined);
        }}
      />
    </div>
  );
}

function GeneratingCard({
  plan,
  hiccup,
  busy,
  onDiscard,
}: {
  plan: MealPlan;
  hiccup: boolean;
  busy: boolean;
  onDiscard: () => void;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 15000);
    return () => clearInterval(t);
  }, []);
  const stuck = now - new Date(plan.created_at).getTime() > STUCK_MS;
  return (
    <Card padding="sm" className="space-y-3">
      <div className="flex items-center gap-3">
        <Loader2 size={18} className="animate-spin text-info shrink-0" />
        <div>
          <div className="text-sm text-content-primary">Jess is planning your week…</div>
          <div className="text-xs text-content-muted">
            Usually 20–60 seconds. You can leave this page — the draft will be waiting.
          </div>
        </div>
      </div>
      {hiccup && <p className="text-xs text-warning">Connection hiccup — still trying…</p>}
      {stuck && (
        <div className="flex items-center gap-2">
          <span className="text-xs text-content-muted flex-1">This is taking much longer than it should.</span>
          <Button size="sm" variant="ghost" disabled={busy} onClick={onDiscard}>
            Cancel it
          </Button>
        </div>
      )}
    </Card>
  );
}

function DraftPlan({
  plan,
  busy,
  swapping,
  sameWeekAsActive,
  buy,
  setBuy,
  onAccept,
  onRedo,
  onDiscard,
  onSwap,
}: {
  plan: MealPlan;
  busy: boolean;
  swapping: boolean;
  sameWeekAsActive: boolean;
  buy: Record<string, boolean>;
  setBuy: (item: string, value: boolean) => void;
  onAccept: (alreadyHave: string[]) => void;
  onRedo: () => void;
  onDiscard: () => void;
  onSwap: (entryId: number) => Promise<void>;
}) {
  // Ticked (default) = buy it. Unticked = "I already have this".
  const alreadyHave = plan.missing.filter((m) => buy[m] === false);
  const buyCount = plan.missing.length - alreadyHave.length;

  return (
    <section className="space-y-3 rounded-xl border-2 border-dashed border-info/40 p-3" aria-label="Draft plan">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="info">Draft — not accepted yet</Badge>
        <h2 className="text-sm font-medium text-content-primary">
          Week of {dayLabel(plan.week_start)} · {plan.profile_label}
        </h2>
      </div>
      <p className="text-xs text-content-muted">
        {sameWeekAsActive
          ? 'Accepting this replaces your current plan for this week (and its reminders).'
          : `Starts ${dayLabel(plan.week_start)}. Nothing is added or scheduled until you accept.`}
      </p>
      <PlanBody plan={plan} busy={busy} swapping={swapping} onSwap={onSwap} />

      {plan.missing.length > 0 && (
        <Card padding="sm" className="space-y-2">
          <div className="text-sm font-medium text-content-primary">Need to buy</div>
          <p className="text-xs text-content-muted">
            Untick anything you already have — Jess marks it as on hand in your pantry instead of adding it to the list.
          </p>
          <ul className="grid grid-cols-1 sm:grid-cols-2 gap-x-3">
            {plan.missing.map((m) => (
              <li key={m}>
                <label className="flex items-center gap-3 min-h-[40px] text-sm">
                  <input
                    type="checkbox"
                    className="h-5 w-5 shrink-0"
                    checked={buy[m] !== false}
                    onChange={(e) => setBuy(m, e.target.checked)}
                  />
                  <span className={buy[m] === false ? 'text-content-muted line-through' : ''}>{m}</span>
                  {buy[m] === false && <span className="text-[11px] text-content-muted">have it</span>}
                </label>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Button variant="primary" className="w-full" disabled={busy} onClick={() => onAccept(alreadyHave)}>
        <Check size={16} /> Accept plan{buyCount ? ` & add ${buyCount} to grocery list` : ''}
      </Button>
      <div className="flex gap-2">
        <Button variant="secondary" className="flex-1" disabled={busy} onClick={onRedo}>
          <RefreshCw size={14} /> Make a different plan
        </Button>
        <Button variant="ghost" disabled={busy} onClick={onDiscard}>
          Discard
        </Button>
      </div>
    </section>
  );
}

function ActivePlan({
  plan,
  collapsedByDefault,
  busy,
  swapping,
  showNewPlan,
  onNewPlan,
  onSwap,
  track,
}: {
  plan: MealPlan;
  collapsedByDefault: boolean;
  busy: boolean;
  swapping: boolean;
  showNewPlan: boolean;
  onNewPlan: () => void;
  onSwap: (entryId: number) => Promise<void>;
  track: Tracker;
}) {
  const [open, setOpen] = useState(!collapsedByDefault);
  useEffect(() => setOpen(!collapsedByDefault), [collapsedByDefault]);
  return (
    <section className="space-y-3" aria-label="Accepted plan">
      <div className="flex items-center justify-between gap-2">
        <button
          className="flex items-center gap-2 min-h-[40px] text-left"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
        >
          {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          <span className="text-sm font-medium text-content-primary">
            Accepted plan · week of {dayLabel(plan.week_start)}
          </span>
        </button>
        {showNewPlan && (
          <Button size="sm" variant="ghost" disabled={busy} onClick={onNewPlan}>
            <RefreshCw size={14} /> New plan
          </Button>
        )}
      </div>
      {open && <PlanBody plan={plan} busy={busy} swapping={swapping} onSwap={onSwap} track={track} />}
    </section>
  );
}

function PlanBody({
  plan,
  busy,
  swapping,
  onSwap,
  track,
}: {
  plan: MealPlan;
  busy: boolean;
  swapping: boolean;
  onSwap: (entryId: number) => Promise<void>;
  /** Only for the accepted plan: cooked / undo / skip / ate buttons. */
  track?: Tracker;
}) {
  const prep = plan.entries.find((e) => e.slot === 'prep');
  const lunches = plan.entries.filter((e) => e.slot === 'lunch');
  const dinners = plan.entries.filter((e) => e.slot === 'dinner');
  return (
    <div className="space-y-3">
      {plan.warnings.length > 0 && (
        <Card padding="sm" className="border-warning/40 space-y-1">
          {plan.warnings.map((w, i) => (
            <p key={i} className="text-xs text-warning flex gap-2">
              <AlertTriangle size={14} className="shrink-0 mt-0.5" aria-hidden />
              <span>{w}</span>
            </p>
          ))}
          <p className="text-[11px] text-content-muted">
            These checks match words in your notes and avoid list — they can miss things. They aren’t medical or allergy
            advice.
          </p>
        </Card>
      )}
      {plan.notes && <p className="text-xs text-content-muted italic">{plan.notes}</p>}
      {prep && plan.recipes[String(prep.recipe_id)] && (
        <MealCard
          key={`${prep.id}-${prep.recipe_id}`}
          label={`${dayLabel(prep.day)} · Batch prep · ${prep.portions} portions`}
          sub={lunches.length ? `Lunch ${lunches.map((l) => dayLabel(l.day).split(' ')[0]).join(', ')}` : undefined}
          entry={prep}
          recipe={plan.recipes[String(prep.recipe_id)]}
          busy={busy}
          swapping={swapping}
          onSwap={onSwap}
          track={track}
          batch={prep.status === 'cooked' ? lunchBatch(plan, lunches) : undefined}
        />
      )}
      {dinners.map((d) =>
        plan.recipes[String(d.recipe_id)] ? (
          <MealCard
            key={`${d.id}-${d.recipe_id}`}
            label={`${dayLabel(d.day)} · Dinner`}
            entry={d}
            recipe={plan.recipes[String(d.recipe_id)]}
            busy={busy}
            swapping={swapping}
            onSwap={onSwap}
            track={track}
          />
        ) : null,
      )}
    </div>
  );
}

function MealCard({
  label,
  sub,
  entry,
  recipe,
  busy,
  swapping,
  onSwap,
  track,
  batch,
}: {
  label: string;
  sub?: string;
  entry: PlanEntry;
  recipe: Recipe;
  busy: boolean;
  swapping: boolean;
  onSwap: (entryId: number) => Promise<void>;
  track?: Tracker;
  batch?: { id: number; portions_made: number; portions_left: number; good_until: string; status: string };
}) {
  const [open, setOpen] = useState(false);
  const [mySwap, setMySwap] = useState(false);
  const [rating, setRating] = useState(recipe.rating);
  const [rateBusy, setRateBusy] = useState(false);
  const have = recipe.ingredients.filter((i) => i.have).length;

  const swap = async () => {
    setMySwap(true);
    try {
      await onSwap(entry.id);
    } finally {
      setMySwap(false);
    }
  };

  const rate = async (r: 'up' | 'down') => {
    const next = (r === 'up' && rating === 5) || (r === 'down' && rating === 1) ? 'clear' : r;
    setRateBusy(true);
    try {
      await planApi.rate(recipe.id, next);
      setRating(next === 'clear' ? null : next === 'up' ? 5 : 1);
    } catch {
      /* rating is a nicety — ignore failures */
    } finally {
      setRateBusy(false);
    }
  };

  const est = [
    recipe.est_kcal != null ? `~${recipe.est_kcal} kcal` : null,
    recipe.est_fiber_g != null ? `${recipe.est_fiber_g}g fibre` : null,
    recipe.est_satfat_g != null ? `${recipe.est_satfat_g}g sat fat` : null,
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <Card padding="sm" className="space-y-2">
      <div className="text-[11px] uppercase tracking-wider text-content-muted">{label}</div>
      <button
        className="w-full flex items-start gap-2 text-left min-h-[40px]"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown size={18} className="mt-0.5 shrink-0" />
        ) : (
          <ChevronRight size={18} className="mt-0.5 shrink-0" />
        )}
        <span className="flex-1 min-w-0">
          <span className="block text-content-primary font-medium">{recipe.name}</span>
          <span className="block text-xs text-content-muted mt-0.5">
            {recipe.minutes ? `${recipe.minutes} min · ` : ''}
            {have}/{recipe.ingredients.length} ingredients on hand
            {sub ? ` · ${sub}` : ''}
          </span>
        </span>
      </button>

      {open && (
        <div className="space-y-3 pl-6">
          <ul className="space-y-1.5 text-sm">
            {recipe.ingredients.map((i) => (
              <li key={i.item} className="flex items-start gap-2">
                {i.have ? (
                  <span className="text-[11px] px-1.5 rounded bg-success/15 text-success shrink-0 mt-0.5">have</span>
                ) : (
                  <span className="text-[11px] px-1.5 rounded bg-warning/15 text-warning shrink-0 mt-0.5">buy</span>
                )}
                <span>
                  {i.item}
                  {i.amount && <span className="text-content-muted"> — {i.amount}</span>}
                </span>
              </li>
            ))}
          </ul>
          <ol className="list-decimal pl-5 space-y-1 text-sm text-content-secondary">
            {recipe.steps.map((s, n) => (
              <li key={n}>{s}</li>
            ))}
          </ol>
          {est && <p className="text-[11px] text-content-muted">Per serving, rough estimate: {est}</p>}
        </div>
      )}

      {track && <TrackRow entry={entry} batch={batch} busy={busy || swapping} track={track} />}
      <div className="flex items-center gap-2">
        <Button
          variant="ghost"
          className="min-h-[40px]"
          disabled={busy || swapping || rateBusy || (track !== undefined && entry.status !== 'planned')}
          onClick={swap}
        >
          {mySwap ? <Loader2 size={14} className="animate-spin" /> : <Shuffle size={14} />}
          {mySwap ? 'Finding something else…' : 'Something else'}
        </Button>
        <span className="flex-1" />
        <Button
          variant="ghost"
          icon
          className={`min-h-[40px] min-w-[40px] ${rating === 5 ? 'text-success' : ''}`}
          disabled={rateBusy || mySwap}
          aria-pressed={rating === 5}
          aria-label="Liked it"
          onClick={() => rate('up')}
        >
          <ThumbsUp size={18} />
        </Button>
        <Button
          variant="ghost"
          icon
          className={`min-h-[40px] min-w-[40px] ${rating === 1 ? 'text-danger' : ''}`}
          disabled={rateBusy || mySwap}
          aria-pressed={rating === 1}
          aria-label="Didn't like it"
          onClick={() => rate('down')}
        >
          <ThumbsDown size={18} />
        </Button>
      </div>
    </Card>
  );
}

interface Tracker {
  cooked: (entryId: number) => void;
  uncook: (entryId: number) => void;
  skip: (entryId: number) => void;
  eat: (batchId: number) => void;
}

/** The batch the plan's lunches eat from, once the prep is cooked. */
function lunchBatch(plan: MealPlan, lunches: PlanEntry[]) {
  const id = lunches.find((l) => l.prep_batch_id)?.prep_batch_id;
  const b = id ? plan.batches?.[String(id)] : undefined;
  return id && b ? { id, ...b } : undefined;
}

function TrackRow({
  entry,
  batch,
  busy,
  track,
}: {
  entry: PlanEntry;
  batch?: { id: number; portions_made: number; portions_left: number; good_until: string; status: string };
  busy: boolean;
  track: Tracker;
}) {
  if (entry.status === 'planned') {
    return (
      <div className="flex flex-wrap gap-2">
        <Button
          variant="success"
          className="min-h-[40px] flex-1"
          disabled={busy}
          onClick={() => track.cooked(entry.id)}
        >
          <Check size={16} /> Cooked it
        </Button>
        {entry.slot === 'dinner' && (
          <Button variant="ghost" className="min-h-[40px]" disabled={busy} onClick={() => track.skip(entry.id)}>
            Skip
          </Button>
        )}
      </div>
    );
  }
  if (entry.status === 'skipped') {
    return (
      <div className="flex items-center gap-2 text-xs text-content-muted">
        <span className="flex-1">Skipped</span>
        <Button variant="ghost" className="min-h-[40px]" disabled={busy} onClick={() => track.cooked(entry.id)}>
          Cooked it after all
        </Button>
      </div>
    );
  }
  const live = batch !== undefined && batch.status === 'active' && batch.portions_left > 0;
  const eaten = batch !== undefined && batch.portions_left < batch.portions_made;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="flex items-center gap-1 text-xs text-success">
        <Check size={14} aria-hidden /> Cooked
      </span>
      {batch && (
        <span className="text-xs text-content-muted">
          {batch.portions_left} portion{batch.portions_left === 1 ? '' : 's'} left · good until{' '}
          {new Date(`${batch.good_until}T12:00:00`).toLocaleDateString([], { weekday: 'short' })}
        </span>
      )}
      <span className="flex-1" />
      {live && (
        <Button variant="secondary" className="min-h-[40px]" disabled={busy} onClick={() => track.eat(batch.id)}>
          Ate one
        </Button>
      )}
      {!eaten && (
        <Button
          variant="ghost"
          className="min-h-[40px]"
          disabled={busy}
          onClick={() => {
            if (
              !batch ||
              window.confirm('Undo the batch prep? This puts the ingredients back and removes the portions.')
            )
              track.uncook(entry.id);
          }}
        >
          Undo cooked
        </Button>
      )}
    </div>
  );
}
