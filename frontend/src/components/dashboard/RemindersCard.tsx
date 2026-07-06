'use client';

import { AlertTriangle, Bell, Check, XCircle } from 'lucide-react';
import { Card, ErrorState, Skeleton } from '@/components/ui';
import { api } from '@/lib/api';
import { useReminders } from '@/lib/hooks';
import type { ReminderOutcome } from '@/lib/types';

const OUTCOME_ORDER: Record<string, number> = { failed: 0, missed: 1, completed: 2 };

export default function RemindersCard() {
  const { data, error, isLoading, mutate } = useReminders();
  const reminders = (data?.reminders ?? []).filter((r) => r.status === 'pending');

  // Trust layer: last-24h delivery log, problems first, capped so the card
  // stays a card.
  const recent = [...(data?.recent ?? [])]
    .sort((a, b) => (OUTCOME_ORDER[a.status] ?? 3) - (OUTCOME_ORDER[b.status] ?? 3))
    .slice(0, 6);
  const undeliveredCount = (data?.recent ?? []).filter(
    (r) => r.status === 'failed' || r.status === 'missed'
  ).length;

  const handleComplete = async (id: string) => {
    // Optimistically drop it, then reconcile with the server.
    const next = data
      ? { ...data, reminders: data.reminders.filter((r) => r.id !== id) }
      : data;
    mutate(next, { revalidate: false });
    try {
      await api.completeReminder(id);
    } finally {
      mutate();
    }
  };

  const formatTime = (iso: string | null | undefined) => {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
    });
  };

  const outcomeLabel = (r: ReminderOutcome) => {
    if (r.status === 'failed') return 'Failed to deliver';
    if (r.status === 'missed') return 'Missed';
    const when = formatTime(r.completed_at);
    if (r.acked_via) return `Done via ${r.acked_via}${when ? ` · ${when}` : ''}`;
    return `Delivered${when ? ` · ${when}` : ''}`;
  };

  const OutcomeIcon = ({ status }: { status: string }) => {
    if (status === 'failed') return <XCircle size={14} className="text-danger shrink-0" />;
    if (status === 'missed') return <AlertTriangle size={14} className="text-warning shrink-0" />;
    return <Check size={14} className="text-success shrink-0" />;
  };

  return (
    <Card>
      <h2 className="text-lg font-semibold text-content-primary mb-3 flex items-center gap-2">
        <Bell size={18} className="text-warning" />
        Reminders
        {reminders.length > 0 && (
          <span className="text-xs bg-warning/20 text-warning px-2 py-0.5 rounded-full">
            {reminders.length}
          </span>
        )}
        {undeliveredCount > 0 && (
          <span className="text-xs bg-danger/20 text-danger px-2 py-0.5 rounded-full">
            {undeliveredCount} not delivered
          </span>
        )}
      </h2>

      {isLoading && (
        <div className="space-y-2">
          {[1, 2].map((i) => (
            <Skeleton key={i} className="h-10" />
          ))}
        </div>
      )}

      {!isLoading && error && (
        <ErrorState compact message="Couldn’t load reminders." onRetry={() => mutate()} />
      )}

      {!isLoading && !error && reminders.length === 0 && (
        <p className="text-sm text-content-muted">No pending reminders</p>
      )}

      {!isLoading && !error && reminders.length > 0 && (
        <div className="space-y-2">
          {reminders.map((r) => (
            <div
              key={r.id}
              className="flex items-center gap-3 p-2.5 rounded-lg bg-surface-raised/40 border border-line/30"
            >
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-white truncate">{r.text}</p>
                <p className="text-xs text-content-muted">{formatTime(r.time)}</p>
              </div>
              <button
                onClick={() => handleComplete(r.id)}
                className="p-1.5 rounded-lg hover:bg-success/20 text-content-muted hover:text-success transition-colors shrink-0"
                title="Complete"
              >
                <Check size={16} />
              </button>
            </div>
          ))}
        </div>
      )}

      {!isLoading && !error && recent.length > 0 && (
        <div className="mt-4 pt-3 border-t border-line/30">
          <p className="text-xs font-medium text-content-muted mb-2">Last 24 h</p>
          <div className="space-y-1.5">
            {recent.map((r) => (
              <div key={r.id} className="flex items-center gap-2 px-1">
                <OutcomeIcon status={r.status} />
                <p className="flex-1 min-w-0 text-xs text-content-secondary truncate">{r.text}</p>
                <span
                  className={`text-xs shrink-0 ${
                    r.status === 'failed'
                      ? 'text-danger'
                      : r.status === 'missed'
                        ? 'text-warning'
                        : 'text-content-muted'
                  }`}
                >
                  {outcomeLabel(r)}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}
