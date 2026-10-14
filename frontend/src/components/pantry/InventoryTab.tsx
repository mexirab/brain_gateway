'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, Minus, Plus, Refrigerator, Trash2, X } from 'lucide-react';
import { Badge, Button, Card, EmptyState, Skeleton } from '@/components/ui';
import { friendlyError } from '@/lib/errors';
import {
  LEVELS,
  LOCATIONS,
  describeAmount,
  pantryApi,
  type Level,
  type PantryItem,
  type PantryLocation,
} from '@/lib/pantry-api';
import AddItemForm from './AddItemForm';

type Filter = 'all' | PantryLocation;

const LOCATION_LABEL: Record<PantryLocation, string> = {
  fridge: 'Fridge',
  freezer: 'Freezer',
  pantry: 'Pantry',
};

export default function InventoryTab({ onStartScan, shown }: { onStartScan: () => void; shown: boolean }) {
  const [items, setItems] = useState<PantryItem[] | null>(null);
  const [filter, setFilter] = useState<Filter>('all');
  const [showAdd, setShowAdd] = useState(false);
  const [showOut, setShowOut] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const all = await pantryApi.items('all');
      setItems(all.filter((i) => i.status !== 'gone'));
      setError(null);
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t load the pantry.'));
    }
  }, []);

  // (Re)load whenever the tab is shown — chat may have changed things.
  useEffect(() => {
    if (shown) load();
  }, [load, shown]);

  const replace = (updated: PantryItem) =>
    setItems((prev) => (prev ? prev.map((i) => (i.id === updated.id ? updated : i)) : prev));

  const run = async (id: number, fn: () => Promise<{ item: PantryItem }>) => {
    setBusyId(id);
    try {
      replace((await fn()).item);
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t update that item.', { preferDetail: true }));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (item: PantryItem) => {
    if (!window.confirm(`Remove ${item.name} from the pantry list? (Use “out” if you just ran out.)`)) return;
    setBusyId(item.id);
    try {
      await pantryApi.remove(item.id);
      setItems((prev) => prev?.filter((i) => i.id !== item.id) ?? prev);
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t remove that item.'));
    } finally {
      setBusyId(null);
    }
  };

  const visible = useMemo(
    () => (items ?? []).filter((i) => filter === 'all' || i.location === filter),
    [items, filter],
  );
  const maybeGone = visible.filter((i) => i.status === 'probably_gone' && !i.is_out);
  const have = visible.filter((i) => i.status === 'active' && !i.is_out);
  const out = visible.filter((i) => i.is_out);

  if (items === null && !error) {
    return (
      <div className="space-y-2">
        {[...Array(4)].map((_, i) => (
          <Skeleton key={i} className="h-14" />
        ))}
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {error && (
        <Card padding="none" className="p-3 text-sm text-danger/80 border-danger/30 flex items-start gap-2">
          <span className="flex-1">{error}</span>
          <Button icon size="sm" variant="ghost" onClick={() => setError(null)} aria-label="Dismiss error">
            <X size={16} />
          </Button>
        </Card>
      )}
      {toast && (
        <Card padding="none" className="p-3 text-sm text-success border-success/30 flex items-start gap-2">
          <span className="flex-1">{toast}</span>
          <Button icon size="sm" variant="ghost" onClick={() => setToast(null)} aria-label="Dismiss">
            <X size={16} />
          </Button>
        </Card>
      )}

      <div className="flex flex-wrap gap-2" aria-label="Filter by location">
        {(['all', ...LOCATIONS] as Filter[]).map((f) => (
          <button
            key={f}
            aria-pressed={filter === f}
            onClick={() => setFilter(f)}
            className={`px-4 py-2 rounded-full text-sm border transition-colors ${
              filter === f
                ? 'bg-brand/20 border-brand/40 text-content-primary font-medium'
                : 'border-line-subtle text-content-muted hover:text-content-primary'
            }`}
          >
            {f === 'all' ? 'All' : LOCATION_LABEL[f]}
          </button>
        ))}
      </div>

      {showAdd ? (
        <AddItemForm
          defaultLocation={filter === 'all' ? 'fridge' : filter}
          onCancel={() => setShowAdd(false)}
          onAdded={(item, merged) => {
            setShowAdd(false);
            setToast(merged ? `Restocked ${item.name}.` : `Added ${item.name}.`);
            load();
          }}
        />
      ) : (
        <Button variant="primary" className="w-full" onClick={() => setShowAdd(true)}>
          <Plus size={16} /> Add item
        </Button>
      )}

      {items && items.length === 0 && !showAdd && (
        <EmptyState
          icon={<Refrigerator size={40} />}
          title="Nothing tracked yet"
          description="Snap a photo of each fridge shelf and Jess will list what she sees for you to check."
          action={
            <Button variant="secondary" onClick={onStartScan}>
              Start a photo stock-take
            </Button>
          }
        />
      )}

      {maybeGone.length > 0 && (
        <section className="space-y-2" aria-label="Maybe gone">
          <h2 className="text-xs uppercase tracking-wider text-warning">Still have these?</h2>
          {maybeGone.map((item) => (
            <Card key={item.id} padding="none" className="p-3 flex items-center gap-2 border-warning/30">
              <span className="flex-1 text-content-primary">{item.name}</span>
              <Button
                size="sm"
                variant="secondary"
                disabled={busyId === item.id}
                onClick={() => run(item.id, () => pantryApi.update(item.id, { status: 'active' }))}
              >
                Still have
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={busyId === item.id}
                onClick={() => run(item.id, () => pantryApi.out(item.id))}
              >
                Gone
              </Button>
            </Card>
          ))}
        </section>
      )}

      {LOCATIONS.filter((loc) => filter === 'all' || filter === loc).map((loc) => {
        const here = have.filter((i) => i.location === loc);
        if (here.length === 0) return null;
        return (
          <section key={loc} className="space-y-2" aria-label={LOCATION_LABEL[loc]}>
            <h2 className="text-xs uppercase tracking-wider text-content-muted">
              {LOCATION_LABEL[loc]} · {here.length}
            </h2>
            {here.map((item) => (
              <ItemRow
                key={item.id}
                item={item}
                busy={busyId === item.id}
                onCount={(delta) =>
                  run(item.id, () =>
                    delta < 0 ? pantryApi.use(item.id, 1) : pantryApi.update(item.id, { qty: (item.qty ?? 0) + 1 }),
                  )
                }
                onLevel={(level) =>
                  run(item.id, () => (level === 'out' ? pantryApi.out(item.id) : pantryApi.update(item.id, { level })))
                }
                onRemove={() => remove(item)}
              />
            ))}
          </section>
        );
      })}

      {out.length > 0 && (
        <section className="space-y-2" aria-label="Out">
          <button
            className="text-xs uppercase tracking-wider text-content-muted hover:text-content-primary"
            onClick={() => setShowOut((s) => !s)}
            aria-expanded={showOut}
          >
            Out · {out.length} {showOut ? '▾' : '▸'}
          </button>
          {showOut &&
            out.map((item) => (
              <Card key={item.id} padding="none" className="p-3 flex items-center gap-2 opacity-80">
                <span className="flex-1 text-content-secondary">{item.name}</span>
                <span className="text-[11px] text-content-muted">{LOCATION_LABEL[item.location]}</span>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={busyId === item.id}
                  onClick={() =>
                    run(item.id, () =>
                      pantryApi.update(item.id, item.track_mode === 'count' ? { qty: 1 } : { level: 'full' }),
                    )
                  }
                >
                  Restocked
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  icon
                  disabled={busyId === item.id}
                  onClick={() => remove(item)}
                  aria-label={`Remove ${item.name}`}
                >
                  <Trash2 size={14} />
                </Button>
              </Card>
            ))}
        </section>
      )}
    </div>
  );
}

function ItemRow({
  item,
  busy,
  onCount,
  onLevel,
  onRemove,
}: {
  item: PantryItem;
  busy: boolean;
  onCount: (delta: number) => void;
  onLevel: (level: Level) => void;
  onRemove: () => void;
}) {
  return (
    <Card padding="none" className="p-3 flex flex-wrap items-center gap-x-3 gap-y-2">
      <div className="flex-1 min-w-[8rem]">
        <div className="text-content-primary truncate">{item.name}</div>
        <div className="text-[11px] text-content-muted mt-0.5 flex gap-2 items-center">
          <Badge tone="neutral">{item.category}</Badge>
          {item.track_mode === 'staple' && <span>staple</span>}
        </div>
      </div>

      {item.track_mode === 'count' && (
        <div className="flex items-center gap-1">
          <Button
            size="sm"
            variant="ghost"
            icon
            disabled={busy}
            onClick={() => onCount(-1)}
            aria-label={`Used one ${item.name}`}
          >
            <Minus size={14} />
          </Button>
          <span className="w-8 text-center font-mono text-sm">{item.qty ?? 0}</span>
          <Button
            size="sm"
            variant="ghost"
            icon
            disabled={busy}
            onClick={() => onCount(1)}
            aria-label={`Add one ${item.name}`}
          >
            <Plus size={14} />
          </Button>
        </div>
      )}

      {item.track_mode === 'level' && (
        <div
          className="flex rounded-lg border border-line-subtle overflow-hidden"
          role="group"
          aria-label={`How full is the ${item.name}`}
        >
          {LEVELS.map((lvl) => (
            <button
              key={lvl}
              disabled={busy}
              onClick={() => onLevel(lvl)}
              aria-pressed={item.level === lvl}
              className={`min-h-[40px] px-2.5 text-xs transition-colors flex items-center gap-0.5 ${
                item.level === lvl
                  ? lvl === 'low'
                    ? 'bg-warning/25 text-content-primary font-semibold'
                    : 'bg-success/25 text-content-primary font-semibold'
                  : 'text-content-muted hover:text-content-primary'
              }`}
            >
              {item.level === lvl && <Check size={12} aria-hidden />}
              {lvl}
            </button>
          ))}
        </div>
      )}

      {item.track_mode === 'staple' && (
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => onLevel('out')}>
          Ran out
        </Button>
      )}

      <span className="sr-only">{describeAmount(item)}</span>
      <Button
        icon
        size="sm"
        variant="ghost"
        onClick={onRemove}
        disabled={busy}
        className="ml-1 text-content-muted hover:text-danger"
        aria-label={`Remove ${item.name}`}
      >
        <Trash2 size={16} />
      </Button>
    </Card>
  );
}
