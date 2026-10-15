'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChefHat, Loader2, Undo2 } from 'lucide-react';
import { Button, Card } from '@/components/ui';
import { friendlyError } from '@/lib/errors';
import { pantryApi, planApi, type OtherCook, type PantryItem } from '@/lib/pantry-api';

const MAX_ITEMS = 30;

function httpStatus(e: unknown): number | undefined {
  const s = (e as { status?: unknown } | null)?.status;
  return typeof s === 'number' ? s : undefined;
}

/** "Cooked something else" — a meal that isn't in the plan: tick the pantry
 *  items it used; they come off in one undoable step and the meal is logged. */
export default function CookedOtherCard({ shown, onChanged }: { shown: boolean; onChanged?: () => void }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<PantryItem[] | null>(null);
  const [recent, setRecent] = useState<OtherCook[]>([]);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [dish, setDish] = useState('');
  const [filter, setFilter] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false); // a double tap must never deduct twice
  const toggleRef = useRef<HTMLButtonElement>(null);

  const loadRecent = useCallback(async () => {
    try {
      setRecent(await planApi.recentOther());
    } catch {
      // The list is a convenience; the form still works without it.
    }
  }, []);

  useEffect(() => {
    if (shown) loadRecent();
  }, [shown, loadRecent]);

  useEffect(() => {
    if (!open || items) return;
    setError(null);
    pantryApi
      .items('active')
      .then((all) => setItems(all.filter((i) => !i.is_out && i.track_mode !== 'staple')))
      .catch((e) => setError(friendlyError(e, 'Couldn’t load the pantry.')));
  }, [open, items]);

  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const list = (items ?? []).filter((i) => !q || i.name.toLowerCase().includes(q));
    return [...list].sort((a, b) => a.name.localeCompare(b.name));
  }, [items, filter]);

  const atMax = picked.size >= MAX_ITEMS;

  const toggle = (id: number) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else if (next.size < MAX_ITEMS) next.add(id);
      return next;
    });

  const reset = () => {
    setPicked(new Set());
    setDish('');
    setFilter('');
  };

  const save = async () => {
    if (!picked.size || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const res = await planApi.cookedOther([...picked], dish.trim());
      const bits = [`Logged ${res.dish || 'your meal'}.`];
      if (res.deducted.length) bits.push(`Used: ${res.deducted.join(', ')}.`);
      if (res.running_low.length) bits.push(`Running low: ${res.running_low.join(', ')}.`);
      setMessage(bits.join(' '));
      reset();
      setItems(null); // amounts changed — reload next time
      setOpen(false);
      toggleRef.current?.focus();
      loadRecent();
      onChanged?.();
    } catch (e) {
      if (httpStatus(e) === 404) {
        setError('Those items were already used up — the list has been refreshed.');
        setPicked(new Set());
        setItems(null);
      } else {
        setError(friendlyError(e, 'Couldn’t log that meal.', { preferDetail: true }));
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  const undo = async (cook: OtherCook) => {
    if (inFlight.current) return;
    if (!window.confirm(`Undo ${cook.dish || 'that meal'}? This puts the items back in the pantry.`)) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const res = await planApi.undoOther(cook.id);
      setMessage(`Undone — ${res.restored} item${res.restored === 1 ? '' : 's'} back in the pantry.`);
      setItems(null);
      loadRecent();
      onChanged?.();
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t undo that.', { preferDetail: true }));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  const undoable = recent.filter((c) => c.can_undo);

  return (
    <Card padding="sm" className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <ChefHat size={16} className="text-content-muted" aria-hidden />
        <h2 className="flex-1 text-sm font-medium text-content-primary">Cooked something not on the plan?</h2>
        <Button
          ref={toggleRef}
          variant={open ? 'ghost' : 'secondary'}
          className="min-h-[40px]"
          onClick={() => {
            if (open) reset();
            setOpen((o) => !o);
            setMessage('');
            setError(null);
          }}
          aria-expanded={open}
        >
          {open ? 'Cancel' : 'Log it'}
        </Button>
      </div>

      {/* Always mounted so VoiceOver announces changes reliably. */}
      <p role="status" className={message ? 'text-sm text-success' : 'sr-only'}>
        {message}
      </p>
      {error && (
        <div role="alert" className="flex flex-wrap items-center gap-2 text-sm text-danger/80">
          <span className="flex-1">{error}</span>
          {open && !items && (
            <Button variant="ghost" size="sm" className="min-h-[40px]" onClick={() => setItems(null)}>
              Try again
            </Button>
          )}
        </div>
      )}

      {open && (
        <div className="space-y-3">
          <input
            className="input w-full min-h-[40px] text-base md:text-sm"
            placeholder="What did you make? (optional)"
            value={dish}
            maxLength={80}
            onChange={(e) => setDish(e.target.value)}
            aria-label="Dish name"
          />
          {/* Save sits above the list so it stays reachable with the iPhone keyboard up. */}
          <div className="sticky top-0 z-10 flex flex-wrap items-center gap-2 bg-surface-raised py-1">
            <Button variant="primary" className="min-h-[40px]" disabled={!picked.size || busy} onClick={save}>
              {busy
                ? 'Saving…'
                : picked.size
                  ? `Log meal (${picked.size} item${picked.size === 1 ? '' : 's'})`
                  : 'Tick what you used'}
            </Button>
            {atMax && <span className="text-xs text-warning">Up to {MAX_ITEMS} items</span>}
          </div>
          <input
            type="search"
            className="input w-full min-h-[40px] text-base md:text-sm"
            placeholder="Find an ingredient"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            aria-label="Filter pantry items"
            enterKeyHint="done"
            autoCorrect="off"
            autoCapitalize="none"
          />
          {!items && !error && (
            <p className="text-xs text-content-muted flex items-center gap-2">
              <Loader2 size={14} className="animate-spin" aria-hidden /> Loading the pantry…
            </p>
          )}
          {items && (
            <fieldset className="max-h-[50vh] overflow-y-auto overscroll-contain space-y-1 rounded-lg border border-line-subtle p-2">
              <legend className="sr-only">Pantry items used</legend>
              {visible.length === 0 && (
                <p className="text-xs text-content-muted p-1">
                  {items.length ? 'Nothing matches.' : 'Nothing in the pantry to use.'}
                </p>
              )}
              {visible.map((i) => {
                const checked = picked.has(i.id);
                return (
                  <label key={i.id} className="flex items-center gap-2 min-h-[40px] px-1 text-sm">
                    <input
                      type="checkbox"
                      className="h-5 w-5"
                      checked={checked}
                      disabled={!checked && atMax}
                      onChange={() => toggle(i.id)}
                    />
                    <span className="flex-1 text-content-primary">{i.name}</span>
                    <span className="text-[11px] text-content-muted">
                      {i.track_mode === 'count' ? `${i.qty ?? 0} left` : i.level}
                    </span>
                  </label>
                );
              })}
            </fieldset>
          )}
          <p className="text-[11px] text-content-muted">
            Each ticked item goes down by one (or one level). Only items you track are listed — staples never run down
            from cooking. To log a meal without any of these, just tell Jess.
          </p>
        </div>
      )}

      {undoable.length > 0 && (
        <ul className="space-y-1" aria-label="Recent off-plan meals">
          {undoable.map((c) => (
            <li key={c.id} className="flex flex-wrap items-center gap-2 text-sm text-content-muted">
              <span className="flex-1">
                {c.dish || 'Off-plan meal'} ·{' '}
                {new Date(c.cooked_at).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' })}
                {c.items.length > 0 && ` · ${c.items.join(', ')}`}
              </span>
              <Button
                variant="ghost"
                size="sm"
                className="min-h-[40px]"
                disabled={busy}
                onClick={() => undo(c)}
                aria-label={`Undo ${c.dish || 'off-plan meal'}`}
              >
                <Undo2 size={14} aria-hidden /> Undo
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
