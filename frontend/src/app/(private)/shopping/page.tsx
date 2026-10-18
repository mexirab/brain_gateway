'use client';

import { useEffect, useRef, useState } from 'react';
import { ShoppingCart, Plus, Trash2, Check, Filter, X } from 'lucide-react';
import { api } from '@/lib/api';
import { Button, Card, EmptyState, ErrorState, Skeleton } from '@/components/ui';
import { friendlyError } from '@/lib/errors';
import { useShopping } from '@/lib/hooks';
import { JUST_BOUGHT, splitShopping } from '@/lib/shopping';
import type { ShoppingItem } from '@/lib/types';

const UNDO_MS = 6000;

export default function ShoppingPage() {
  const [newItem, setNewItem] = useState('');
  const [listName, setListName] = useState('grocery');
  const [filter, setFilter] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [lastBought, setLastBought] = useState<ShoppingItem | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const pending = useRef(0);

  // Always fetch bought items too: they stay visible under "Bought" so a
  // mis-tap in the store is one tap to undo.
  const { data: items = [], error: loadError, isLoading, mutate } = useShopping(true);

  useEffect(() => {
    if (!lastBought) return;
    const t = setTimeout(() => setLastBought(null), UNDO_MS);
    return () => clearTimeout(t);
  }, [lastBought]);

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = newItem.trim();
    if (!trimmed) return;
    setAdding(true);
    setActionError(null);
    try {
      await api.addShoppingItem(trimmed, listName);
      setNewItem('');
      mutate();
      inputRef.current?.focus();
    } catch (err) {
      setActionError(friendlyError(err, 'Couldn’t add that item.'));
    } finally {
      setAdding(false);
    }
  };

  const toggle = async (it: ShoppingItem) => {
    const buying = !it.checked;
    setActionError(null);
    setLastBought(buying ? it : null);
    pending.current += 1;
    // Optimistic move. Functional form so several quick taps don't overwrite
    // each other; revalidate only once the last tap has been saved.
    mutate(
      (cur) =>
        cur?.map((x) =>
          x.id === it.id ? { ...x, checked: buying ? 1 : 0, checked_at: buying ? JUST_BOUGHT : null } : x,
        ),
      { revalidate: false },
    );
    try {
      if (buying) await api.checkShoppingItem(it.id);
      else await api.uncheckShoppingItem(it.id);
    } catch (err) {
      setActionError(friendlyError(err, 'Couldn’t update that item.'));
    } finally {
      pending.current -= 1;
      if (pending.current === 0) mutate();
    }
  };

  const handleDelete = async (it: ShoppingItem) => {
    setActionError(null);
    mutate((cur) => cur?.filter((x) => x.id !== it.id), { revalidate: false });
    try {
      await api.deleteShoppingItem(it.id);
    } catch (err) {
      setActionError(friendlyError(err, 'Couldn’t delete that item.'));
    } finally {
      mutate();
    }
  };

  const handleClearBought = async (count: number) => {
    const scope = filter ? ` from the ${filter} list` : '';
    if (!window.confirm(`Remove all bought items${scope}? (${count} shown here)`)) return;
    setActionError(null);
    try {
      await api.clearCheckedItems(filter || undefined);
      mutate();
    } catch (err) {
      setActionError(friendlyError(err, 'Couldn’t clear bought items.'));
    }
  };

  // Get unique list names for filter
  const listNames = Array.from(new Set(items.map((it) => it.list_name))).sort();
  const filtered = filter ? items.filter((it) => it.list_name === filter) : items;
  const { toBuy, bought } = splitShopping(filtered);

  return (
    <div className="max-w-2xl mx-auto space-y-6 pb-20">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-content-primary flex items-center gap-3">
          <ShoppingCart size={24} className="text-success" />
          Shopping List
        </h1>
        <span className="text-sm text-content-muted">
          {toBuy.length} to buy
        </span>
      </div>

      {/* Add item form */}
      <form onSubmit={handleAdd} className="glass p-4 flex gap-3">
        <input
          ref={inputRef}
          type="text"
          value={newItem}
          onChange={(e) => setNewItem(e.target.value)}
          placeholder="Add an item..."
          className="input flex-1"
          disabled={adding}
        />
        <select value={listName} onChange={(e) => setListName(e.target.value)} className="input w-auto shrink-0">
          <option value="grocery">Grocery</option>
          <option value="shopping">Shopping</option>
          <option value="hardware">Hardware</option>
          <option value="pharmacy">Pharmacy</option>
          <option value="other">Other</option>
        </select>
        <Button type="submit" variant="primary" disabled={adding || !newItem.trim()}>
          <Plus size={16} />
          Add
        </Button>
      </form>

      {/* Filters */}
      {listNames.length > 1 && (
        <div className="flex flex-wrap gap-2 items-center">
          <Filter size={14} className="text-content-muted" />
          <button
            onClick={() => setFilter(null)}
            className={`px-3 py-1 rounded-full text-xs transition-colors ${
              filter === null
                ? 'bg-brand-500/20 text-brand-400 border border-brand-500/30'
                : 'text-content-muted hover:text-content-primary border border-line-subtle'
            }`}
          >
            All
          </button>
          {listNames.map((name) => (
            <button
              key={name}
              onClick={() => setFilter(filter === name ? null : name)}
              className={`px-3 py-1 rounded-full text-xs transition-colors capitalize ${
                filter === name
                  ? 'bg-success/20 text-success border border-success/30'
                  : 'text-content-muted hover:text-content-primary border border-line-subtle'
              }`}
            >
              {name}
            </button>
          ))}
        </div>
      )}

      {/* Loading / Error */}
      {isLoading && (
        <div className="space-y-2">
          {[...Array(4)].map((_, i) => (
            <Skeleton key={i} className="h-12" />
          ))}
        </div>
      )}
      {!isLoading && loadError && <ErrorState compact message="Couldn’t load your list." onRetry={() => mutate()} />}
      {actionError && (
        <p role="alert" className="text-sm text-danger/70">
          {actionError}
        </p>
      )}

      {!isLoading && !loadError && (
        <>
          <section aria-label="To buy" className="space-y-2">
            {toBuy.length === 0 && bought.length === 0 && (
              <EmptyState
                icon={<ShoppingCart size={32} />}
                title="Your list is empty"
                description="Add items above or tell Jess to add something."
              />
            )}
            {toBuy.length === 0 && bought.length > 0 && (
              <EmptyState
                icon={<Check size={32} />}
                title="Everything’s in the cart"
                description="Tap an item under Bought to put it back on the list."
              />
            )}
            {toBuy.map((it) => (
              <ItemRow key={it.id} it={it} onToggle={toggle} onDelete={handleDelete} />
            ))}
          </section>

          {/* Bought: tap a row to move it back up */}
          {bought.length > 0 && (
            <section aria-labelledby="bought-heading" className="space-y-2 pt-6 border-t border-line-subtle">
              <h2
                id="bought-heading"
                className="text-sm font-semibold text-content-secondary uppercase tracking-wider mb-1"
              >
                Bought ({bought.length})
              </h2>
              {bought.map((it) => (
                <ItemRow key={it.id} it={it} onToggle={toggle} onDelete={handleDelete} />
              ))}
              <div className="flex justify-end pt-4">
                <Button variant="danger" size="sm" onClick={() => handleClearBought(bought.length)}>
                  <Trash2 size={12} />
                  {filter ? `Clear bought (${filter})` : 'Clear bought'}
                </Button>
              </div>
            </section>
          )}
        </>
      )}

      {/* Undo bar for the last tick: the row moved away from under the finger */}
      {lastBought && (
        <div className="fixed inset-x-0 bottom-20 md:bottom-6 z-40 flex justify-center px-4 pointer-events-none">
          <Card
            padding="none"
            role="status"
            className="pointer-events-auto p-2 pl-4 flex items-center gap-3 text-sm shadow-lg max-w-md w-full"
          >
            <span className="flex-1 min-w-0 truncate">
              Bought <span className="font-medium">{lastBought.item}</span>
            </span>
            <Button
              size="sm"
              variant="ghost"
              className="min-h-[44px]"
              onClick={() => {
                const it = lastBought;
                setLastBought(null);
                toggle({ ...it, checked: 1 });
              }}
            >
              Undo
            </Button>
          </Card>
        </div>
      )}
    </div>
  );
}

function ItemRow({
  it,
  onToggle,
  onDelete,
}: {
  it: ShoppingItem;
  onToggle: (it: ShoppingItem) => void;
  onDelete: (it: ShoppingItem) => void;
}) {
  const bought = Boolean(it.checked);
  return (
    <Card padding="none" className="flex items-center group">
      {/* The whole row is the tick target (≥ 48 px), not just the small square. */}
      <button
        type="button"
        role="checkbox"
        aria-checked={bought}
        onClick={() => onToggle(it)}
        className="flex-1 min-w-0 min-h-[48px] p-3 flex items-center gap-3 text-left rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
      >
        <span
          aria-hidden
          className={`w-6 h-6 rounded-md border-2 flex items-center justify-center shrink-0 transition-colors ${
            bought ? 'border-success/50 bg-success/20' : 'border-line-strong group-hover:border-success'
          }`}
        >
          {bought && <Check size={14} className="text-success" />}
        </span>
        <span className={`flex-1 min-w-0 break-words ${bought ? 'opacity-60' : ''}`}>
          <span className={bought ? 'text-content-secondary line-through' : 'text-content-primary'}>{it.item}</span>
          {it.amount && <span className="block text-sm text-content-muted tabular-nums">{it.amount}</span>}
        </span>
        {it.list_name !== 'grocery' && (
          <span className="text-[10px] text-content-muted capitalize px-2 py-0.5 rounded-full border border-line-subtle shrink-0">
            {it.list_name}
          </span>
        )}
      </button>
      <button
        type="button"
        onClick={() => onDelete(it)}
        aria-label={`Delete ${it.item}`}
        className="shrink-0 self-stretch px-3 text-content-muted hover:text-danger transition-all sm:opacity-0 sm:group-hover:opacity-100 focus-visible:opacity-100"
      >
        <X size={16} />
      </button>
    </Card>
  );
}
