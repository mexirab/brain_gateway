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
import { Card, Button } from '@/components/ui';

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
    } catch {
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
    } catch {
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
    } catch {
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
        <Loader2 className="animate-spin text-brand" size={32} />
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-content-primary">Settings</h1>
        <p className="text-sm text-content-muted mt-0.5">
          YNAB integration and category mapping
        </p>
      </div>

      {/* Connection Status */}
      <Card>
        <h3 className="text-sm font-semibold text-content-primary uppercase tracking-wider mb-3 flex items-center gap-2">
          <Link2 size={14} />
          YNAB Connection
        </h3>

        {!status?.configured ? (
          <div className="text-center py-4">
            <XCircle size={32} className="text-content-muted mx-auto mb-2" />
            <p className="text-content-secondary text-sm">YNAB not configured</p>
            <p className="text-content-muted text-xs mt-1">
              Set <code className="bg-surface-raised px-1 rounded">YNAB_ACCESS_TOKEN</code> in your .env
              file to enable auto-tracking
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {/* Status indicator */}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                {status.connected ? (
                  <CheckCircle size={18} className="text-success" />
                ) : (
                  <XCircle size={18} className="text-danger" />
                )}
                <span
                  className={`text-sm font-medium ${status.connected ? 'text-success' : 'text-danger'}`}
                >
                  {status.connected ? 'Connected' : 'Disconnected'}
                </span>
              </div>
              {status.budget_name && (
                <span className="text-sm text-content-secondary">
                  Budget: <span className="text-content-primary">{status.budget_name}</span>
                </span>
              )}
            </div>

            {/* Sync info */}
            <div className="flex items-center justify-between text-sm">
              <span className="text-content-muted">
                Last sync:{' '}
                {status.last_synced_at
                  ? new Date(status.last_synced_at).toLocaleString()
                  : 'Never'}
              </span>
              <span className="text-content-muted">
                {status.discretionary_count} / {status.category_count} categories
                mapped
              </span>
            </div>

            {/* Action buttons */}
            <div className="flex gap-2">
              <Button
                onClick={handleSync}
                disabled={syncing}
                className="gap-1.5"
              >
                {syncing ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <RefreshCw size={14} />
                )}
                {syncing ? 'Syncing...' : 'Sync Now'}
              </Button>
              <Button
                variant="secondary"
                onClick={handleResetSync}
                disabled={resetting}
                className="gap-1.5"
              >
                {resetting ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <RotateCcw size={14} />
                )}
                Reset Sync
              </Button>
            </div>

            {/* Sync result */}
            {syncResult && (
              <p
                className={`text-sm ${syncResult.startsWith('Error') || syncResult.includes('failed') ? 'text-danger' : 'text-success'}`}
              >
                {syncResult}
              </p>
            )}
          </div>
        )}
      </Card>

      {/* Category Mapping */}
      {status?.configured && status?.connected && groups.length > 0 && (
        <Card>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-content-primary uppercase tracking-wider flex items-center gap-2">
              <Settings size={14} />
              Category Mapping
            </h3>
            {hasPendingChanges && (
              <Button
                variant="primary"
                size="sm"
                onClick={saveMappings}
                disabled={savingMapping}
                className="gap-1.5"
              >
                {savingMapping ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <CheckCircle size={12} />
                )}
                Save Changes
              </Button>
            )}
          </div>
          <p className="text-xs text-content-muted mb-4">
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
                  className="border border-line-subtle rounded-lg overflow-hidden"
                >
                  {/* Group header */}
                  <button
                    onClick={() => toggleGroup(group.group_name)}
                    className="w-full flex items-center justify-between p-3 hover:bg-surface-raised/50 transition-colors text-left"
                  >
                    <div className="flex items-center gap-2">
                      {expanded ? (
                        <ChevronDown size={14} className="text-content-muted" />
                      ) : (
                        <ChevronRight size={14} className="text-content-muted" />
                      )}
                      <span className="text-sm font-medium text-content-primary">
                        {group.group_name}
                      </span>
                      <span className="text-xs text-content-muted">
                        ({group.categories.length})
                      </span>
                    </div>
                    {discCount > 0 && (
                      <span className="text-xs text-brand bg-brand/10 px-2 py-0.5 rounded-full">
                        {discCount} discretionary
                      </span>
                    )}
                  </button>

                  {/* Category list */}
                  {expanded && (
                    <div className="border-t border-line-subtle">
                      {group.categories.map((cat) => {
                        const isDisc = getCategoryValue(
                          cat.name,
                          cat.is_discretionary,
                        );
                        const hasChange = cat.name in pendingChanges;

                        return (
                          <label
                            key={cat.name}
                            className={`flex items-center justify-between p-3 cursor-pointer hover:bg-surface-raised/30 transition-colors ${hasChange ? 'bg-brand/5' : ''}`}
                          >
                            <div className="flex items-center gap-3">
                              <input
                                type="checkbox"
                                checked={isDisc}
                                onChange={() =>
                                  toggleCategory(cat.name, isDisc)
                                }
                                className="w-4 h-4 accent-brand rounded"
                              />
                              <span className="text-sm text-content-primary">
                                {cat.name}
                              </span>
                            </div>
                            <div className="flex items-center gap-4 text-xs text-content-muted">
                              {cat.activity > 0 && (
                                <span>
                                  Spent: {formatCurrency(cat.activity)}
                                </span>
                              )}
                              {cat.balance !== 0 && (
                                <span
                                  className={
                                    cat.balance < 0
                                      ? 'text-danger'
                                      : 'text-success'
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
            <div className="mt-4 flex items-center justify-between p-3 rounded-lg bg-brand/10 border border-brand/20">
              <span className="text-sm text-brand">
                {Object.keys(pendingChanges).length} unsaved change
                {Object.keys(pendingChanges).length !== 1 ? 's' : ''}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setPendingChanges({})}
                >
                  Discard
                </Button>
                <Button
                  size="sm"
                  onClick={saveMappings}
                  disabled={savingMapping}
                  className="gap-1 py-1"
                >
                  {savingMapping ? (
                    <Loader2 size={10} className="animate-spin" />
                  ) : null}
                  Save
                </Button>
              </div>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
