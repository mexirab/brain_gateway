'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import {
  ShoppingCart,
  Plus,
  Trash2,
  Check,
  RotateCcw,
  Filter,
  X,
} from 'lucide-react';
import { api } from '@/lib/api';
import type { ShoppingItem } from '@/lib/types';

export default function ShoppingPage() {
  const [items, setItems] = useState<ShoppingItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newItem, setNewItem] = useState('');
  const [listName, setListName] = useState('grocery');
  const [filter, setFilter] = useState<string | null>(null);
  const [showChecked, setShowChecked] = useState(false);
  const [adding, setAdding] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const fetchItems = useCallback(() => {
    api
      .shoppingList(undefined, showChecked)
      .then((data) => {
        setItems(data);
        setError(null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [showChecked]);

  useEffect(() => {
    fetchItems();
    const interval = setInterval(fetchItems, 10000);
    return () => clearInterval(interval);
  }, [fetchItems]);

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = newItem.trim();
    if (!trimmed) return;
    setAdding(true);
    try {
      await api.addShoppingItem(trimmed, listName);
      setNewItem('');
      fetchItems();
      inputRef.current?.focus();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add');
    } finally {
      setAdding(false);
    }
  };

  const handleCheck = async (id: number, currentlyChecked: boolean) => {
    // Optimistic update
    setItems((prev) =>
      prev.map((it) =>
        it.id === id ? { ...it, checked: currentlyChecked ? 0 : 1 } : it,
      ),
    );
    try {
      if (currentlyChecked) {
        await api.uncheckShoppingItem(id);
      } else {
        await api.checkShoppingItem(id);
      }
    } catch (err) {
      // Revert on failure
      setItems((prev) =>
        prev.map((it) =>
          it.id === id ? { ...it, checked: currentlyChecked ? 1 : 0 } : it,
        ),
      );
      setError(err instanceof Error ? err.message : 'Failed to update');
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await api.deleteShoppingItem(id);
      setItems((prev) => prev.filter((it) => it.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete');
    }
  };

  const handleClearChecked = async () => {
    try {
      await api.clearCheckedItems(filter || undefined);
      fetchItems();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to clear');
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
        <h1 className="text-2xl font-bold text-zinc-200 flex items-center gap-3">
          <ShoppingCart size={24} className="text-emerald-400" />
          Shopping List
        </h1>
        <span className="text-sm text-zinc-500">
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
          className="flex-1 bg-transparent border border-zinc-700 rounded-lg px-3 py-2 text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-brand-500"
          disabled={adding}
        />
        <select
          value={listName}
          onChange={(e) => setListName(e.target.value)}
          className="bg-surface-overlay border border-zinc-700 rounded-lg px-3 py-2 text-zinc-300 text-sm focus:outline-none focus:border-brand-500"
        >
          <option value="grocery">Grocery</option>
          <option value="shopping">Shopping</option>
          <option value="hardware">Hardware</option>
          <option value="pharmacy">Pharmacy</option>
          <option value="other">Other</option>
        </select>
        <button
          type="submit"
          disabled={adding || !newItem.trim()}
          className="flex items-center gap-2 px-4 py-2 bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 rounded-lg hover:bg-emerald-500/30 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
        >
          <Plus size={16} />
          Add
        </button>
      </form>

      {/* Filters */}
      {listNames.length > 1 && (
        <div className="flex flex-wrap gap-2 items-center">
          <Filter size={14} className="text-zinc-600" />
          <button
            onClick={() => setFilter(null)}
            className={`px-3 py-1 rounded-full text-xs transition-colors ${
              filter === null
                ? 'bg-brand-500/20 text-brand-400 border border-brand-500/30'
                : 'text-zinc-500 hover:text-zinc-300 border border-zinc-800'
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
                  ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                  : 'text-zinc-500 hover:text-zinc-300 border border-zinc-800'
              }`}
            >
              {name}
            </button>
          ))}
          <label className="ml-auto flex items-center gap-2 text-xs text-zinc-500 cursor-pointer">
            <input
              type="checkbox"
              checked={showChecked}
              onChange={(e) => setShowChecked(e.target.checked)}
              className="rounded border-zinc-600"
            />
            Show checked
          </label>
        </div>
      )}

      {/* Loading / Error */}
      {loading && (
        <div className="space-y-2">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="h-12 bg-zinc-800/30 rounded-lg animate-pulse" />
          ))}
        </div>
      )}
      {error && <p className="text-sm text-red-400/70">{error}</p>}

      {/* Unchecked items */}
      {!loading && !error && (
        <div className="space-y-2">
          {unchecked.length === 0 && checked.length === 0 && (
            <p className="text-sm text-zinc-600 text-center py-12">
              Your list is empty. Add items above or tell Jess to add something.
            </p>
          )}
          {unchecked.map((it) => (
            <div
              key={it.id}
              className="glass p-3 flex items-center gap-3 group"
            >
              <button
                onClick={() => handleCheck(it.id, Boolean(it.checked))}
                className="w-6 h-6 rounded-md border-2 border-zinc-600 hover:border-emerald-400 flex items-center justify-center transition-colors shrink-0"
              >
                {/* empty checkbox */}
              </button>
              <span className="flex-1 text-zinc-200">{it.item}</span>
              {it.list_name !== 'grocery' && (
                <span className="text-[10px] text-zinc-600 capitalize px-2 py-0.5 rounded-full border border-zinc-800">
                  {it.list_name}
                </span>
              )}
              <button
                onClick={() => handleDelete(it.id)}
                className="opacity-0 group-hover:opacity-100 text-zinc-600 hover:text-red-400 transition-all"
              >
                <X size={14} />
              </button>
            </div>
          ))}

          {/* Checked items */}
          {showChecked && checked.length > 0 && (
            <>
              <div className="flex items-center justify-between pt-4">
                <span className="text-xs text-zinc-600 uppercase tracking-wider">
                  Checked ({checked.length})
                </span>
                <button
                  onClick={handleClearChecked}
                  className="flex items-center gap-1 text-xs text-zinc-600 hover:text-red-400 transition-colors"
                >
                  <Trash2 size={12} />
                  Clear checked
                </button>
              </div>
              {checked.map((it) => (
                <div
                  key={it.id}
                  className="glass p-3 flex items-center gap-3 opacity-50 group"
                >
                  <button
                    onClick={() => handleCheck(it.id, true)}
                    className="w-6 h-6 rounded-md border-2 border-emerald-500/50 bg-emerald-500/20 flex items-center justify-center transition-colors shrink-0"
                  >
                    <Check size={14} className="text-emerald-400" />
                  </button>
                  <span className="flex-1 text-zinc-400 line-through">
                    {it.item}
                  </span>
                  {it.list_name !== 'grocery' && (
                    <span className="text-[10px] text-zinc-700 capitalize px-2 py-0.5 rounded-full border border-zinc-800">
                      {it.list_name}
                    </span>
                  )}
                  <button
                    onClick={() => handleDelete(it.id)}
                    className="opacity-0 group-hover:opacity-100 text-zinc-600 hover:text-red-400 transition-all"
                  >
                    <X size={14} />
                  </button>
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}
