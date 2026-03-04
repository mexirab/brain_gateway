'use client';

import { useState, useEffect, useCallback } from 'react';
import {
  Loader2,
  RefreshCw,
  CheckCircle,
  XCircle,
  Link2,
  Settings,
  ChevronDown,
  ChevronRight,
  RotateCcw,
} from 'lucide-react';
import { financeApi } from '@/lib/finance-api';
import { formatCurrency } from '@/lib/finance-utils';

interface YnabStatus {
  configured: boolean;
  connected: boolean;
  budget_id: string | null;
  budget_name: string | null;
  last_synced_at: string | null;
  server_knowledge: number | null;
  category_count: number;
  discretionary_count: number;
}

interface CategoryGroup {
  group_name: string;
  categories: Array<{
    name: string;
    is_discretionary: boolean;
    budgeted: number;
    activity: number;
    balance: number;
  }>;
}

export default function SettingsPage() {
  const [status, setStatus] = useState<YnabStatus | null>(null);
  const [groups, setGroups] = useState<CategoryGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [savingMapping, setSavingMapping] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [syncResult, setSyncResult] = useState<string | null>(null);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const [pendingChanges, setPendingChanges] = useState<Record<string, boolean>>(
    {},
  );

  const loadData = useCallback(async () => {
    try {
      const statusRes = await financeApi.getYnabStatus();
      setStatus(statusRes);

      if (statusRes.configured && statusRes.connected) {
        const catRes = await financeApi.getYnabCategories();
        setGroups(catRes.groups);
        // Auto-expand groups that have discretionary categories
        const expanded = new Set<string>();
        for (const g of catRes.groups) {
          if (g.categories.some((c) => c.is_discretionary)) {
            expanded.add(g.group_name);
          }
        }
        setExpandedGroups(expanded);
      }
    } catch (err) {
      console.error('Failed to load YNAB status:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  async function handleSync() {
    setSyncing(true);
    setSyncResult(null);
    try {
      const result = await financeApi.triggerYnabSync();
      if (result.error) {
        setSyncResult(`Error: ${result.error}`);
      } else {
        setSyncResult(`Synced ${result.synced} transactions`);
      }
      // Reload status
      const statusRes = await financeApi.getYnabStatus();
      setStatus(statusRes);
    } catch (err) {
      setSyncResult('Sync failed');
    } finally {
      setSyncing(false);
    }
  }

  async function handleResetSync() {
    if (!confirm('This will delete all YNAB transactions and re-sync from scratch. Continue?')) {
      return;
    }
    setResetting(true);
    try {
      await financeApi.resetYnabSync();
      setSyncResult('Sync reset. Click "Sync Now" to re-import.');
      const statusRes = await financeApi.getYnabStatus();
      setStatus(statusRes);
    } catch (err) {
      setSyncResult('Reset failed');
    } finally {
      setResetting(false);
    }
  }

  function toggleCategory(catName: string, currentValue: boolean) {
    setPendingChanges((prev) => ({
      ...prev,
      [catName]: !currentValue,
    }));
  }

  function getCategoryValue(catName: string, originalValue: boolean): boolean {
    return catName in pendingChanges ? pendingChanges[catName] : originalValue;
  }

  const hasPendingChanges = Object.keys(pendingChanges).length > 0;

  async function saveMappings() {
    if (!hasPendingChanges) return;
    setSavingMapping(true);
    try {
      await financeApi.updateCategoryMapping(pendingChanges);
      setPendingChanges({});
      // Reload categories to reflect new state
      const catRes = await financeApi.getYnabCategories();
      setGroups(catRes.groups);
      setSyncResult('Category mappings saved. Budget recalculated.');
      // Reload status for updated counts
      const statusRes = await financeApi.getYnabStatus();
      setStatus(statusRes);
    } catch (err) {
      setSyncResult('Failed to save mappings');
    } finally {
      setSavingMapping(false);
    }
  }

  function toggleGroup(groupName: string) {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(groupName)) next.delete(groupName);
      else next.add(groupName);
      return next;
    });
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <Loader2 className="animate-spin text-brand-500" size={32} />
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-zinc-100">Settings</h1>
        <p className="text-sm text-zinc-500 mt-0.5">
          YNAB integration and category mapping
        </p>
      </div>

      {/* Connection Status */}
      <div className="glass p-5">
        <h3 className="text-sm font-semibold text-zinc-300 uppercase tracking-wider mb-3 flex items-center gap-2">
          <Link2 size={14} />
          YNAB Connection
        </h3>

        {!status?.configured ? (
          <div className="text-center py-4">
            <XCircle size={32} className="text-zinc-600 mx-auto mb-2" />
            <p className="text-zinc-400 text-sm">YNAB not configured</p>
            <p className="text-zinc-600 text-xs mt-1">
              Set <code className="bg-zinc-800 px-1 rounded">YNAB_ACCESS_TOKEN</code> in your .env
              file to enable auto-tracking
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {/* Status indicator */}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                {status.connected ? (
                  <CheckCircle size={18} className="text-emerald-400" />
                ) : (
                  <XCircle size={18} className="text-red-400" />
                )}
                <span
                  className={`text-sm font-medium ${status.connected ? 'text-emerald-400' : 'text-red-400'}`}
                >
                  {status.connected ? 'Connected' : 'Disconnected'}
                </span>
              </div>
              {status.budget_name && (
                <span className="text-sm text-zinc-400">
                  Budget: <span className="text-zinc-300">{status.budget_name}</span>
                </span>
              )}
            </div>

            {/* Sync info */}
            <div className="flex items-center justify-between text-sm">
              <span className="text-zinc-500">
                Last sync:{' '}
                {status.last_synced_at
                  ? new Date(status.last_synced_at).toLocaleString()
                  : 'Never'}
              </span>
              <span className="text-zinc-500">
                {status.discretionary_count} / {status.category_count} categories
                mapped
              </span>
            </div>

            {/* Action buttons */}
            <div className="flex gap-2">
              <button
                onClick={handleSync}
                disabled={syncing}
                className="flex items-center gap-1.5 px-4 py-2 bg-brand-600 hover:bg-brand-700 disabled:opacity-50 text-white text-sm font-medium rounded-lg transition-colors"
              >
                {syncing ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <RefreshCw size={14} />
                )}
                {syncing ? 'Syncing...' : 'Sync Now'}
              </button>
              <button
                onClick={handleResetSync}
                disabled={resetting}
                className="flex items-center gap-1.5 px-4 py-2 bg-zinc-800 hover:bg-zinc-700 disabled:opacity-50 text-zinc-300 text-sm font-medium rounded-lg transition-colors border border-zinc-700"
              >
                {resetting ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <RotateCcw size={14} />
                )}
                Reset Sync
              </button>
            </div>

            {/* Sync result */}
            {syncResult && (
              <p
                className={`text-sm ${syncResult.startsWith('Error') || syncResult.includes('failed') ? 'text-red-400' : 'text-emerald-400'}`}
              >
                {syncResult}
              </p>
            )}
          </div>
        )}
      </div>

      {/* Category Mapping */}
      {status?.configured && status?.connected && groups.length > 0 && (
        <div className="glass p-5">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-zinc-300 uppercase tracking-wider flex items-center gap-2">
              <Settings size={14} />
              Category Mapping
            </h3>
            {hasPendingChanges && (
              <button
                onClick={saveMappings}
                disabled={savingMapping}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white text-xs font-medium rounded-lg transition-colors"
              >
                {savingMapping ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <CheckCircle size={12} />
                )}
                Save Changes
              </button>
            )}
          </div>
          <p className="text-xs text-zinc-500 mb-4">
            Check the categories that count as discretionary spending (depletes
            your $1K health bar)
          </p>

          <div className="space-y-1">
            {groups.map((group) => {
              const expanded = expandedGroups.has(group.group_name);
              const discCount = group.categories.filter((c) =>
                getCategoryValue(c.name, c.is_discretionary),
              ).length;

              return (
                <div
                  key={group.group_name}
                  className="border border-zinc-800 rounded-lg overflow-hidden"
                >
                  {/* Group header */}
                  <button
                    onClick={() => toggleGroup(group.group_name)}
                    className="w-full flex items-center justify-between p-3 hover:bg-zinc-800/50 transition-colors text-left"
                  >
                    <div className="flex items-center gap-2">
                      {expanded ? (
                        <ChevronDown size={14} className="text-zinc-500" />
                      ) : (
                        <ChevronRight size={14} className="text-zinc-500" />
                      )}
                      <span className="text-sm font-medium text-zinc-300">
                        {group.group_name}
                      </span>
                      <span className="text-xs text-zinc-600">
                        ({group.categories.length})
                      </span>
                    </div>
                    {discCount > 0 && (
                      <span className="text-xs text-brand-400 bg-brand-500/10 px-2 py-0.5 rounded-full">
                        {discCount} discretionary
                      </span>
                    )}
                  </button>

                  {/* Category list */}
                  {expanded && (
                    <div className="border-t border-zinc-800">
                      {group.categories.map((cat) => {
                        const isDisc = getCategoryValue(
                          cat.name,
                          cat.is_discretionary,
                        );
                        const hasChange = cat.name in pendingChanges;

                        return (
                          <label
                            key={cat.name}
                            className={`flex items-center justify-between p-3 cursor-pointer hover:bg-zinc-800/30 transition-colors ${hasChange ? 'bg-brand-500/5' : ''}`}
                          >
                            <div className="flex items-center gap-3">
                              <input
                                type="checkbox"
                                checked={isDisc}
                                onChange={() =>
                                  toggleCategory(cat.name, isDisc)
                                }
                                className="w-4 h-4 accent-brand-500 rounded"
                              />
                              <span className="text-sm text-zinc-300">
                                {cat.name}
                              </span>
                            </div>
                            <div className="flex items-center gap-4 text-xs text-zinc-500">
                              {cat.activity > 0 && (
                                <span>
                                  Spent: {formatCurrency(cat.activity)}
                                </span>
                              )}
                              {cat.balance !== 0 && (
                                <span
                                  className={
                                    cat.balance < 0
                                      ? 'text-red-400'
                                      : 'text-emerald-400'
                                  }
                                >
                                  {formatCurrency(cat.balance)}
                                </span>
                              )}
                            </div>
                          </label>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* Unsaved changes indicator */}
          {hasPendingChanges && (
            <div className="mt-4 flex items-center justify-between p-3 rounded-lg bg-brand-500/10 border border-brand-500/20">
              <span className="text-sm text-brand-400">
                {Object.keys(pendingChanges).length} unsaved change
                {Object.keys(pendingChanges).length !== 1 ? 's' : ''}
              </span>
              <div className="flex gap-2">
                <button
                  onClick={() => setPendingChanges({})}
                  className="text-xs text-zinc-400 hover:text-zinc-300"
                >
                  Discard
                </button>
                <button
                  onClick={saveMappings}
                  disabled={savingMapping}
                  className="flex items-center gap-1 px-3 py-1 bg-brand-600 hover:bg-brand-700 disabled:opacity-50 text-white text-xs font-medium rounded-lg transition-colors"
                >
                  {savingMapping ? (
                    <Loader2 size={10} className="animate-spin" />
                  ) : null}
                  Save
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
