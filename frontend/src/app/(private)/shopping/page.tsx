'use client';

import { useState, useRef } from 'react';
import {
  ShoppingCart,
  Plus,
  Trash2,
  Check,
  Filter,
  X,
} from 'lucide-react';
import { api } from '@/lib/api';
import { Card, Button, ErrorState } from '@/components/ui';
import { friendlyError } from '@/lib/errors';
import { useShopping } from '@/lib/hooks';

export default function ShoppingPage() {
  const [newItem, setNewItem] = useState('');
  const [listName, setListName] = useState('grocery');
  const [filter, setFilter] = useState<string | null>(null);
  const [showChecked, setShowChecked] = useState(false);
  const [adding, setAdding] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const { data: items = [], error: loadError, isLoading, mutate } = useShopping(showChecked);

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

  const handleCheck = async (id: number, currentlyChecked: boolean) => {
    setActionError(null);
    // Optimistic toggle, then reconcile with the server.
    mutate(
      items.map((it) => (it.id === id ? { ...it, checked: currentlyChecked ? 0 : 1 } : it)),
      { revalidate: false },
    );
    try {
      if (currentlyChecked) {
        await api.uncheckShoppingItem(id);
      } else {
        await api.checkShoppingItem(id);
      }
    } catch (err) {
      setActionError(friendlyError(err, 'Couldn’t update that item.'));
    } finally {
      mutate();
    }
  };

  const handleDelete = async (id: number) => {
    setActionError(null);
    mutate(items.filter((it) => it.id !== id), { revalidate: false });
    try {
      await api.deleteShoppingItem(id);
    } catch (err) {
      setActionError(friendlyError(err, 'Couldn’t delete that item.'));
    } finally {
      mutate();
    }
  };

  const handleClearChecked = async () => {
    setActionError(null);
    try {
      await api.clearCheckedItems(filter || undefined);
      mutate();
    } catch (err) {
      setActionError(friendlyError(err, 'Couldn’t clear checked items.'));
    }
  };

  // Get unique list names for filter
  const listNames = Array.from(new Set(items.map((it) => it.list_name))).sort();
  const filtered = filter
    ? items.filter((it) => it.list_name === filter)
    : items;
  const unchecked = filtered.filter((it) => !it.checked);
  const checked = filtered.filter((it) => it.checked);

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-content-primary flex items-center gap-3">
          <ShoppingCart size={24} className="text-success" />
          Shopping List
        </h1>
        <span className="text-sm text-content-muted">
          {unchecked.length} item{unchecked.length !== 1 ? 's' : ''}
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
        <select
          value={listName}
          onChange={(e) => setListName(e.target.value)}
          className="input"
        >
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
          <label className="ml-auto flex items-center gap-2 text-xs text-content-muted cursor-pointer">
            <input
              type="checkbox"
              checked={showChecked}
              onChange={(e) => setShowChecked(e.target.checked)}
              className="rounded border-line-strong"
            />
            Show checked
          </label>
        </div>
      )}

      {/* Loading / Error */}
      {isLoading && (
        <div className="space-y-2">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="h-12 bg-surface-raised/30 rounded-lg animate-pulse" />
          ))}
        </div>
      )}
      {!isLoading && loadError && (
        <ErrorState compact message="Couldn’t load your list." onRetry={() => mutate()} />
      )}
      {actionError && <p className="text-sm text-danger/70">{actionError}</p>}

      {/* Unchecked items */}
      {!isLoading && !loadError && (
        <div className="space-y-2">
          {unchecked.length === 0 && checked.length === 0 && (
            <p className="text-sm text-content-muted text-center py-12">
              Your list is empty. Add items above or tell Jess to add something.
            </p>
          )}
          {unchecked.map((it) => (
            <Card
              key={it.id}
              padding="none"
              className="p-3 flex items-center gap-3 group"
            >
              <button
                onClick={() => handleCheck(it.id, Boolean(it.checked))}
                className="w-6 h-6 rounded-md border-2 border-line-strong hover:border-success flex items-center justify-center transition-colors shrink-0"
              >
                {/* empty checkbox */}
              </button>
              <span className="flex-1 text-content-primary">{it.item}</span>
              {it.list_name !== 'grocery' && (
                <span className="text-[10px] text-content-muted capitalize px-2 py-0.5 rounded-full border border-line-subtle">
                  {it.list_name}
                </span>
              )}
              <button
                onClick={() => handleDelete(it.id)}
                className="opacity-0 group-hover:opacity-100 text-content-muted hover:text-danger transition-all"
              >
                <X size={14} />
              </button>
            </Card>
          ))}

          {/* Checked items */}
          {showChecked && checked.length > 0 && (
            <>
              <div className="flex items-center justify-between pt-4">
                <span className="text-xs text-content-muted uppercase tracking-wider">
                  Checked ({checked.length})
                </span>
                <Button variant="danger" size="sm" onClick={handleClearChecked}>
                  <Trash2 size={12} />
                  Clear checked
                </Button>
              </div>
              {checked.map((it) => (
                <Card
                  key={it.id}
                  padding="none"
                  className="p-3 flex items-center gap-3 opacity-50 group"
                >
                  <button
                    onClick={() => handleCheck(it.id, true)}
                    className="w-6 h-6 rounded-md border-2 border-success/50 bg-success/20 flex items-center justify-center transition-colors shrink-0"
                  >
                    <Check size={14} className="text-success" />
                  </button>
                  <span className="flex-1 text-content-secondary line-through">
                    {it.item}
                  </span>
                  {it.list_name !== 'grocery' && (
                    <span className="text-[10px] text-content-muted capitalize px-2 py-0.5 rounded-full border border-line-subtle">
                      {it.list_name}
                    </span>
                  )}
                  <button
                    onClick={() => handleDelete(it.id)}
                    className="opacity-0 group-hover:opacity-100 text-content-muted hover:text-danger transition-all"
                  >
                    <X size={14} />
                  </button>
                </Card>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}
